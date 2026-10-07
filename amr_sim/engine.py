"""AMR 3대 창고 시뮬레이션 엔진 (CASE1 · CASE2 공통).

시간 전진형: 1 tick = 0.1초. 각 AMR 는 칸 단위로 상하좌우 이동(대각선 없음)하며 한 칸은 한 대만 점유한다.
좁은 통로는 구역 전체를 한 번에 1대만 점유한다. 두 CASE 는 창고·주문·작업시간·RFID 결과·작업자 수가
모두 같고, 아래 두 부품만 다르다.
  - dispatcher : 작업배정·순서 (CASE1 단순 순차 / CASE2 OR-Tools VRP)
  - planner    : 경로 실행 (CASE1 독립 A* + 충돌 시 정지·대기 / CASE2 Cooperative A* + 예약테이블)

작업 흐름 (stage) — 주문 1건 = 입고 작업 + 출고 작업 (출고는 입고 완료 후)
  입고: TO_IN → AT_IN(태그 대기·적재) → TO_RFID → AT_RFID(인식, 실패 시 작업자 개입) → TO_RACK → AT_RACK(적치+WMS)
  출고: TO_PICK → AT_PICK(피킹+WMS) → TO_OUT(중앙교차로·좁은 통로) → AT_OUT(하역+WMS) → 주문 완료
  작업 완료 후
    CASE1 (chaining=False): RETURN(작업장=입고장 복귀) → AT_BASE 에서 다음 작업 배정 (없으면 대기장)
    CASE2 (chaining=True) : 그 자리에서 바로 후속 작업 배정 (없으면 대기장으로 가는 중에도 배정 가능)

CASE1 충돌 처리: 다음 칸이 점유돼 있으면 정지·대기 (같은 tick 동시 요청은 AMR1>AMR2>AMR3 순).
  대기 후 출발 시 재출발 지연 1초. 막은 AMR 가 멈춰 있으면 2초 후 다른 AMR 칸을 피해 재계획,
  마주 보고 10초 이상 막히면 옆 칸으로 비켜섬 (교착 해소 → 1000건 완주 보장).
CASE2: 예약테이블에 따라 계획된 시각에 출발. 목적지가 작업 중이면 작업 종료 예상 시각에 맞춰 도착.

시간 분류 (AMR별, 매 tick 하나 → 합계 = 전체 시간)
  move 이동 / work 작업(적재·인식·적치·피킹·하역·WMS·작업자 개입) / wait 대기(다른 AMR·병목 때문에 멈춤,
  재출발 지연 포함) / tag 태그 부착 대기 / idle 유휴(배정된 주문 없음) / down 고장(방해요소)
"""
from __future__ import annotations

import random
from collections import deque

from . import params as P
from .flow import remaining_ticks, service_ticks, task_end_cell
from .jobs import Inventory, Order, Task
from .layout import (AMR_HOMES, INBOUND_STATION, NARROW_PASSAGE, OUTBOUND_STATION, RACKS, RFID_GATE, ZONES,
                     Grid, build_grid, render_ascii)
from .metrics import AMRStats, OrderRecord, SimResult
from .motion import path_ticks, traverse_ticks
from .pathfinding import distance, static_path
from .problems import ProblemDetector

T = P.ticks
REPLAN_AFTER = 20        # tick (CASE1 막힘 후 재계획)
SIDESTEP_AFTER = 100     # tick (CASE1 마주 보고 막힘 → 비켜서기)
PLAN_RETRY = 10          # tick (CASE2 경로 없음 → 재시도)
REDISPATCH_EVERY = 50    # tick (대기 주문 + 빈 AMR 가 있을 때 재배정 주기)
MISMATCH_SIDESTEP = 3
RESOLVE_COOLDOWN = 100   # tick: 해결 직후 10초 안의 같은 쌍·같은 위치 충돌은 같은 상황으로 봄    # CASE2 예약 어긋남이 움직임 없이 3번 반복되면 비켜서기


class AMR:
    def __init__(self, idx: int, home):
        self.idx = idx
        self.name = f"AMR{idx + 1}"
        self.cell = self.home = home
        self.job: Task | None = None          # 현재 수행 중인 작업 (입고 또는 출고)
        self.last_end = None                   # 직전 작업을 끝낸 칸 (연속 수행 판정)
        self.base_since = 0
        self.rec: OrderRecord | None = None
        self.stage = "IDLE"
        self.goal = None
        self.rack_cell = None
        self.slots = (None, None)
        self.path: list | None = None          # CASE1 남은 칸
        self.schedule: deque | None = None     # CASE2 [(출발 tick, 칸, 이동 tick)]
        self.retry_at = 0
        self.saved_goal = None                 # CASE2 비켜서기 중 원래 목적지
        self.mismatch_streak = 0               # CASE2 움직이지 못한 채 연속된 예약 어긋남 횟수
        self.frozen_until = 0                  # 충돌 → 작업자 해결 중이면 이 tick 까지 정지
        self.resolve_sidestep = False          # 정면 충돌 해결: 작업자가 이 AMR 를 옆으로 비켜 세움
        self.next_cell = None
        self.move_left = 0
        self.move_dur = 1
        self.heading = None
        self.loaded = False
        self.tasks: deque = deque()
        self.after_tasks = None
        self.waiting = False
        self.wait_start = 0
        self.last_replan = 0
        self.restart_left = 0
        # 이번 tick 문제 판정 신호
        self.attempt = None
        self.blocker: AMR | None = None
        self.wait_target = None
        self.category = "idle"
        self.stats = AMRStats(self.name)

    @property
    def pos(self):
        return self.next_cell or self.cell

    def status(self) -> str:
        if self.category == "down":
            s = "DOWN"
        elif self.category == "human":
            s = "HUMAN"
        elif self.waiting or self.restart_left > 0:
            s = "WAIT"
        elif self.tasks:
            s = self.tasks[0][0]
        elif self.stage == "AT_IN":
            s = "TAG"
        else:
            s = {"IDLE": "IDLE", "TO_HOME": "HOME", "RETURN": "RETURN", "AT_BASE": "BASE"}.get(self.stage, "MOVE")
        j = self.job.label if self.job else "-"
        return f"{self.name}:{j}/{s}"


class Simulation:
    def __init__(self, name: str, orders: list[Order], dispatcher, planner, n_amr: int = P.AMR_COUNT,
                 grid: Grid | None = None, disturbances=(), seed: int = P.SEED, chaining: bool = False):
        self.name = name
        self.chaining = chaining      # True: 작업 완료 위치에서 바로 후속 작업 (CASE2) / False: 매번 작업장 복귀 (CASE1)
        self.jobs = sorted(orders, key=lambda j: (j.release, j.job_id))
        self.grid = grid or build_grid()
        self.dispatcher = dispatcher
        self.planner = planner
        self.coop = getattr(planner, "cooperative", False)
        self.amrs = [AMR(i, AMR_HOMES[i]) for i in range(n_amr)]
        self.occupied = {a.cell: a for a in self.amrs}
        if self.coop:
            for a in self.amrs:
                planner.park(a.idx, a.cell, 0)
        self.inventory = Inventory(seed)
        self.disturbances = list(disturbances)
        for d in self.disturbances:
            d.attach(self)
        self.detector = ProblemDetector(self.on_collision)
        self.human_rng = random.Random(seed + 3)   # 충돌 해결시간 전용 (CASE 별로 충돌 순서대로 뽑음)
        self.collisions: list[dict] = []
        self.resolved: dict = {}                   # (AMR 쌍, 위치) → 같은 충돌로 보는 마지막 tick
        self.queue: list[Task] = []   # 대기 작업 (입고 작업 + 선행조건을 충족한 출고 작업)
        self.records: dict[int, OrderRecord] = {}
        self.tag_ready: dict[int, int] = {}
        self.tagger_free = 0
        self.now = 0
        self.next_job = 0
        self.done = 0
        self.dirty = False
        self.last_dispatch = -10 ** 9
        # 시스템 집계
        self.max_queue = 0            # 최대 대기 주문 수 (아직 입고 작업이 시작되지 않은 주문)
        self.max_tasks = 0            # 최대 대기 작업 수 (입고 + 출고)
        self.mismatch_replans = 0     # CASE2: 예약 어긋남 → 재계획
        self.rfid = {"OK": 0, "FAIL": 0, "MISREAD": 0}
        self.worker_ticks = {"태그부착": 0, "RFID 오류": 0, "수동 배차 확인": 0, "충돌 해결": 0}
        self.rfid_interventions = 0

    # ------------------------------------------------------------ 주문 발생 → 작업 생성
    def release_jobs(self):
        while self.next_job < len(self.jobs) and T(self.jobs[self.next_job].release) <= self.now:
            o = self.jobs[self.next_job]
            self.records[o.job_id] = OrderRecord(o.job_id, o.order_id, o.rack, T(o.release))
            tag = T(o.t_tag)            # 입고장 작업자가 도착순으로 RFID 태그 부착
            self.tagger_free = max(self.tagger_free, T(o.release)) + tag
            self.tag_ready[o.job_id] = self.tagger_free
            self.worker_ticks["태그부착"] += tag
            self.add_task(Task(o, "IN", self.now))
            self.next_job += 1

    def add_task(self, task: Task):
        """대기 작업 목록에 추가 (우선순위 = 주문 발생순, 같은 주문은 입고 → 출고)."""
        self.queue.append(task)
        self.queue.sort(key=lambda t: t.priority)
        self.dirty = True

    # ------------------------------------------------------------ 배정
    def operational(self, a: AMR) -> bool:
        return all(d.amr_available(self, a) for d in self.disturbances)

    def task_feasible(self, task: Task) -> bool:
        """후속 작업 후보 조건: 대기 목록에 있다 = 존재·미완료·미배정·선행조건 충족 (출고는 입고 완료 후에만 생성).
        여기서 추가로 실제 화물/빈 저장 위치를 확인한다. 금지칸·장애물·경로·충돌은 경로 계획이 보장."""
        if task.kind == "IN":
            return self.inventory.can_place(task.rack)
        return self.inventory.can_pick(task.rack)

    def available_amrs(self):
        """배정 가능한 AMR: 작업 없음 + 운용 중. CASE1 은 작업장(입고장) 복귀를 마쳐야 배정 가능."""
        return [a for a in self.amrs if a.job is None and a.stage != "RETURN" and self.operational(a)]

    def projected_state(self, a: AMR):
        """배정 계산용: AMR 가 현재 작업을 끝내고 다음 작업을 시작할 수 있는 위치와 예상 tick (자유주행)."""
        if a.job is None:
            if a.stage == "RETURN":
                return INBOUND_STATION, self.now + path_ticks(a.pos, INBOUND_STATION, False)
            return a.pos, self.now
        end, free = task_end_cell(a), self.now + remaining_ticks(a)
        if not self.chaining:     # CASE1: 작업 후 반드시 작업장 복귀
            free += path_ticks(end, INBOUND_STATION, False)
            end = INBOUND_STATION
        return end, free

    def task_cost(self, a: AMR, task: Task) -> tuple[int, int]:
        """(예상 수행비용 tick, 작업 시작 위치까지 거리 m): 지금 이 AMR 에 작업을 주면 완료까지 걸릴 예상시간."""
        pos, free = self.projected_state(a)
        return (free - self.now) + path_ticks(pos, task.start_cell, False) + service_ticks(task), \
            distance(pos, task.start_cell)

    def dispatch(self):
        available = self.available_amrs()
        if not (self.queue and available):
            return
        if not self.dirty and self.now - self.last_dispatch < REDISPATCH_EVERY:
            return
        self.dirty = False
        self.last_dispatch = self.now
        candidates = [t for t in self.queue if self.task_feasible(t)]
        if not candidates:
            return
        for task, a in self.dispatcher.assign(self, self.now, candidates, available):
            self.queue.remove(task)
            self.start_task(a, task)

    def start_task(self, a: AMR, task: Task):
        # 배차 비효율 판정: 선택 가능 AMR(운용 중 전부, 바쁜 AMR 는 끝나는 시각 기준) 중 최소 비용과 비교
        costs = {b.name: self.task_cost(b, task) for b in self.amrs if self.operational(b)}
        rec = self.records[task.job_id]
        cost, dist = costs[a.name]
        best = min(costs, key=lambda k: costs[k][0])
        min_cost, best_dist = costs[best]
        chained = a.last_end is not None and a.pos == a.last_end and a.stage != "AT_BASE"
        rec.set_assign(task.kind, self.now, a.name, dist, cost, min_cost, best, chained)
        if cost > min_cost:
            first = not rec.inefficient
            rec.inefficient = True
            rec.extra_time += cost - min_cost
            rec.extra_dist += max(0, dist - best_dist)
            if cost >= min_cost * (1 + P.DISPATCH_CHECK_RATIO) and not rec.manual_check:
                rec.manual_check = True
                self.worker_ticks["수동 배차 확인"] += T(P.DISPATCH_CHECK_SEC)
        task.amr = a.name
        a.job, a.rec = task, rec
        a.stats.chained += chained
        a.rack_cell, a.slots = None, (None, None)
        if task.kind == "IN":
            self.set_goal(a, INBOUND_STATION, "TO_IN")
        else:                       # 출고: 출고 위치 선정(FIFO 실제 화물) 후 그 슬롯 앞으로
            pick = self.inventory.choose_pick(task.rack)
            a.slots = (None, pick)
            a.rack_cell = RACKS[task.rack].slot_access(int(pick.split("-")[1]))
            self.set_goal(a, a.rack_cell, "TO_PICK")

    def set_goal(self, a: AMR, goal, stage: str):
        a.goal, a.stage, a.path, a.schedule, a.saved_goal = goal, stage, None, None, None

    # ------------------------------------------------------------ 작업 흐름
    def run_tasks(self, a: AMR, stage: str, tasks, after):
        a.stage = stage
        a.tasks.extend([list(t) for t in tasks])
        a.after_tasks = after

    def on_arrive(self, a: AMR):
        task, rec = a.job, a.rec
        if a.stage == "TO_HOME":
            a.stage, a.goal = "IDLE", None
            return
        if a.stage == "RETURN":         # CASE1: 작업장(입고장) 복귀 완료 → 배정 대기
            a.stage, a.goal = "AT_BASE", None
            a.base_since = self.now
            a.stats.returns += 1
            self.dirty = True
            return
        o = task.order
        if a.stage == "TO_IN":
            rec.t_in = self.now
            a.stage = "AT_IN"           # 태그 부착 완료를 기다린 뒤 적재
        elif a.stage == "TO_RFID":
            tasks = [("RFID", T(P.RFID_READ))]
            self.rfid[o.rfid] += 1
            if o.rfid == "FAIL":        # 인식 실패 → 작업자 수동 확인
                self.rfid_interventions += 1
                self.worker_ticks["RFID 오류"] += T(P.WORKER_INTERVENTION)
                tasks.append(("HELP", T(P.WORKER_INTERVENTION)))
            self.run_tasks(a, "AT_RFID", tasks, self.after_rfid)
        elif a.stage == "TO_RACK":
            tasks = [("PLACE", T(o.t_place)), ("WMS", T(P.RFID_WMS))]
            self.run_tasks(a, "AT_RACK", tasks, self.after_place)
        elif a.stage == "TO_PICK":
            tasks = [("PICK", T(o.t_pick)), ("WMS", T(P.RFID_WMS))]
            for d in self.disturbances:
                tasks += d.rack_tasks(self, a, o)
            self.run_tasks(a, "AT_PICK", tasks, self.after_pick)
        elif a.stage == "TO_OUT":
            self.run_tasks(a, "AT_OUT", [("UNLOAD", T(o.t_unload)), ("WMS", T(P.RFID_WMS))], self.after_unload)

    def after_load(self, a: AMR):
        a.loaded = True
        self.set_goal(a, RFID_GATE, "TO_RFID")

    def after_rfid(self, a: AMR):
        a.rec.t_rfid = self.now
        place = self.inventory.choose_place(a.job.rack)     # 입고 위치 선정 (빈 슬롯)
        a.slots = (place, None)
        a.rack_cell = RACKS[a.job.rack].slot_access(int(place.split("-")[1])) if place else a.job.order.rack_cell
        self.set_goal(a, a.rack_cell, "TO_RACK")

    def after_place(self, a: AMR):
        rec = a.rec
        rec.t_rack = self.now
        rec.slot_in = a.slots[0]
        rec.tote_in = self.inventory.store(a.job.order, a.slots[0])
        a.stats.tasks_in += 1
        self.add_task(Task(a.job.order, "OUT", self.now))    # 선행조건 충족 → 출고 작업 생성
        self.finish_task(a)

    def after_pick(self, a: AMR):
        rec = a.rec
        rec.t_pick = self.now
        rec.slot_out = a.slots[1]
        rec.tote_out = self.inventory.retrieve(a.job.rack, a.slots[1])
        a.loaded = True
        self.set_goal(a, OUTBOUND_STATION, "TO_OUT")

    def after_unload(self, a: AMR):
        a.rec.complete = self.now
        a.stats.jobs += 1
        a.stats.tasks_out += 1
        self.done += 1
        for d in self.disturbances:
            d.on_order_done(self, a, a.job.order)
        self.finish_task(a)

    def finish_task(self, a: AMR):
        """작업 완료. CASE1: 반드시 작업장(입고장) 복귀. CASE2: 이 위치에서 바로 다음 작업 선택 가능."""
        a.last_end = a.cell
        a.job, a.rec, a.loaded = None, None, False
        self.dirty = True
        if self.chaining:
            self.set_goal(a, a.home, "TO_HOME")    # 다음 배정 전까지만 — 배정되면 그 자리에서 바로 출발
        else:
            self.set_goal(a, INBOUND_STATION, "RETURN")

    # ------------------------------------------------------------ 점유·대기
    def holder(self, cell, a: AMR):
        """cell 에 들어가지 못하게 막는 AMR (좁은 통로는 구역 전체 1대)."""
        o = self.occupied.get(cell)
        if o is not None and o is not a:
            return o
        if cell in NARROW_PASSAGE:
            for b in self.amrs:
                if b is not a and (b.cell in NARROW_PASSAGE or b.next_cell in NARROW_PASSAGE):
                    return b
        return None

    def mark_wait(self, a: AMR, target=None):
        a.wait_target = target
        if not a.waiting:
            a.waiting = True
            a.wait_start = self.now
            a.stats.stops += 1
        return "wait"

    def begin_move(self, a: AMR, nxt, dur: int):
        self.occupied[nxt] = a
        a.next_cell = nxt
        a.move_left = a.move_dur = dur
        a.waiting = False
        a.attempt = a.blocker = None
        a.mismatch_streak = 0

    # ------------------------------------------------------------ CASE1: 독립 A*
    def step_independent(self, a: AMR) -> str:
        if not a.path:
            a.path = self.planner.plan(a.cell, a.goal)
        nxt = a.path[0]
        owner = self.holder(nxt, a)
        if owner is None:
            a.path.pop(0)
            was_waiting = a.waiting
            self.begin_move(a, nxt, traverse_ticks(a.cell, nxt, a.heading, a.loaded))
            if was_waiting:
                a.restart_left = P.RESTART_TICKS
            return "go"
        if not a.waiting:
            a.last_replan = self.now
        a.attempt, a.blocker = nxt, owner
        self.mark_wait(a, nxt)
        if owner.next_cell is None and self.now - a.last_replan >= REPLAN_AFTER:
            a.last_replan = self.now
            avoid = {c for c, o in self.occupied.items() if o is not a}
            p = self.planner.plan(a.cell, a.goal, avoid)
            if p and self.holder(p[0], a) is None:
                a.path = p
                return "retry"
            if self.now - a.wait_start >= SIDESTEP_AFTER and owner.waiting and owner.blocker is a:
                owner_next = set(owner.path[:2]) if owner.path else set()
                for n in self.grid.neighbors(a.cell):
                    if self.holder(n, a) is None and n not in owner_next:
                        a.path = [n]
                        return "retry"
        return "wait"

    # ------------------------------------------------------------ CASE2: 예약테이블
    def step_cooperative(self, a: AMR) -> str:
        if not a.schedule:
            if self.now < a.retry_at:
                return self.mark_wait(a, a.goal)
            moves = self.planner.plan_timed(a.idx, a.cell, a.goal, self.now, a.heading, a.loaded,
                                            goal_release=self.expected_release(a.goal))
            if moves is None:
                a.retry_at = self.now + PLAN_RETRY
                if self.in_goal_cycle(a):
                    self.step_aside(a)
                return self.mark_wait(a, a.goal)
            a.schedule = deque(moves)
        dep, nxt, dur = a.schedule[0]
        if self.now < dep:
            ahead = [n for _, n, _ in list(a.schedule)[:3]]
            target = next((n for n in ahead if n in self._zone_cells), nxt)
            return self.mark_wait(a, target)
        owner = self.holder(nxt, a)
        if owner is not None or self.now > dep:
            # 앞 AMR 가 계획보다 늦게 떠나 예약이 어긋남 → 멈추고 재계획 (물리적 충돌은 엔진이 막음)
            self.mismatch_replans += 1
            a.schedule = None
            a.attempt, a.blocker = nxt, owner
            a.mismatch_streak += 1
            if a.mismatch_streak >= MISMATCH_SIDESTEP and a.saved_goal is None:
                # 두 AMR 가 서로의 현재 칸을 지나가려는 계획을 반복해서 무효화 (스왑 교착) → 한쪽이 비켜선다
                a.mismatch_streak = 0
                self.step_aside(a)
            return self.mark_wait(a, nxt)
        a.schedule.popleft()
        self.begin_move(a, nxt, dur)
        return "go"

    _zone_cells = frozenset(c for z in ZONES.values() for c in z)

    def expected_release(self, cell):
        """cell 을 차지하고 작업 중인 AMR 가 떠날 것으로 예상되는 tick (모르면 None)."""
        h = self.occupied.get(cell)
        if h is None or h.next_cell is not None:
            return None
        if h.tasks:
            return self.now + sum(t[1] for t in h.tasks) + P.RESTART_TICKS
        if h.stage == "AT_IN" and h.job:
            rem = max(0, self.tag_ready[h.job.job_id] - self.now) + T(h.job.order.t_load)
            return self.now + rem + P.RESTART_TICKS
        return None

    # ------------------------------------------------------------ 충돌 → 작업자 개입 (인력 의존도)
    def on_collision(self, ep):
        """경로/정면 충돌 1건(에피소드) = 작업자 개입 1회. 해결시간 20~60초 랜덤 (seed 고정).

        기본(기록 모드): 모든 충돌 에피소드를 1건씩 기록만 하고 AMR 는 멈추지 않는다
        (tools/collisions_from_events.py 의 사후 계산과 같은 규칙).
        정지 모드(HUMAN_RESOLVE_STOPS=True): 작업자가 이미 그 AMR 를 처리 중이거나 같은 쌍·위치를 막 해결한 직후
        (10초 이내)의 재발은 같은 상황으로 보고 새 개입으로 세지 않는다."""
        amrs = [a for a in self.amrs if a.name in ep.amrs]
        key = (tuple(sorted(ep.amrs)), ep.where)
        if P.HUMAN_RESOLVE_STOPS and (any(self.now < a.frozen_until for a in amrs)
                                      or self.now < self.resolved.get(key, -1)):
            return      # (정지 모드) 작업자가 처리 중인 상황의 반복은 1건으로
        t = T(self.human_rng.uniform(P.HUMAN_RESOLVE_MIN, P.HUMAN_RESOLVE_MAX))
        self.collisions.append({
            "collision_id": len(self.collisions) + 1, "time": ep.start * P.TICK,
            "amr_1": ep.amrs[0], "amr_2": ep.amrs[1] if len(ep.amrs) > 1 else "",
            "collision_type": ep.kind, "location": ep.where,
            "human_intervention_time": t * P.TICK, "orders": " ".join(sorted(ep.orders)),
        })
        self.worker_ticks["충돌 해결"] += t
        end = self.now + 1 + t
        self.resolved[key] = end + RESOLVE_COOLDOWN
        if not P.HUMAN_RESOLVE_STOPS:
            return
        for a in amrs:                          # 1) AMR 이동 정지 2) 작업자 개입 3) 해결 4) 동시에 재개
            a.frozen_until = end
        if ep.kind == "정면충돌" and amrs:
            max(amrs, key=lambda a: a.idx).resolve_sidestep = True   # 우선순위 낮은 쪽을 비켜 세움

    def resolve_head_on(self, a: AMR):
        if self.coop:
            if a.saved_goal is None and a.goal is not None:
                self.step_aside(a)
            return
        other_next = set()
        for b in self.amrs:
            if b is not a and b.path:
                other_next.update(b.path[:2])
        for n in self.grid.neighbors(a.cell):
            if self.holder(n, a) is None and n not in other_next:
                a.path = [n]
                return

    def in_goal_cycle(self, a: AMR) -> bool:
        """a 의 목적지를 점유한 AMR → 그 AMR 의 목적지를 점유한 AMR → ... 가 a 로 돌아오면 교착."""
        cur, seen = a, set()
        while cur is not None and cur.idx not in seen:
            seen.add(cur.idx)
            holder = self.occupied.get(cur.goal) if cur.goal else None
            if holder is None or holder is cur or holder.tasks or holder.stage == "AT_IN" or holder.schedule:
                return False
            if holder is a:
                return True
            cur = holder
        return False

    def step_aside(self, a: AMR):
        """교착 해소: 다른 AMR 목적지·경로와 겹치지 않는 가장 가까운 빈 칸으로 잠시 비켜선다."""
        banned = set(NARROW_PASSAGE)
        for o in self.amrs:
            if o.goal:
                banned.add(o.goal)
                if o is not a:
                    banned.update(static_path(o.cell, o.goal))
        seen, frontier = {a.cell}, [a.cell]
        while frontier:
            nxt = []
            for c in frontier:
                for n in self.grid.neighbors(c):
                    if n in seen:
                        continue
                    seen.add(n)
                    if (n not in banned and n not in self.occupied
                            and not self.planner.table.held_forever_by_other(n, a.idx)):
                        a.saved_goal = a.saved_goal or a.goal
                        a.goal, a.schedule, a.retry_at = n, None, self.now
                        self.planner.asides += 1
                        return
                    nxt.append(n)
            frontier = nxt

    # ------------------------------------------------------------ 한 tick
    def step(self, a: AMR) -> str:
        a.attempt = a.blocker = a.wait_target = None
        if not all(d.amr_can_move(self, a) for d in self.disturbances):
            return "down"
        if self.now < a.frozen_until:          # 충돌 → 작업자가 해결하는 동안 정지
            return "human"
        if a.resolve_sidestep:                 # 해결 완료: 정면 충돌이면 이 AMR 를 옆 칸으로 비켜 세운 뒤 재개
            a.resolve_sidestep = False
            self.resolve_head_on(a)
        for _ in range(20):
            if a.tasks:
                task = a.tasks[0]
                task[1] -= 1
                if task[1] <= 0:
                    a.tasks.popleft()
                    if not a.tasks:
                        a.after_tasks(a)
                return "work"
            if a.stage == "AT_IN":
                if self.now >= self.tag_ready[a.job.job_id]:
                    self.run_tasks(a, "AT_IN", [("LOAD", T(a.job.order.t_load))], self.after_load)
                    continue
                return "tag"
            if a.next_cell is not None:
                if a.restart_left > 0:
                    a.restart_left -= 1
                    a.wait_target = a.next_cell
                    return "wait"
                a.move_left -= 1
                if a.move_left <= 0:
                    del self.occupied[a.cell]
                    a.heading = (a.next_cell[0] - a.cell[0], a.next_cell[1] - a.cell[1])
                    a.cell, a.next_cell = a.next_cell, None
                    a.stats.distance += 1
                    if a.rec:
                        a.rec.distance += 1
                return "move"
            if a.goal is None:
                return "idle"
            if a.cell == a.goal:
                if a.saved_goal is not None:       # 비켜서기 완료 → 원래 목적지로
                    a.goal, a.saved_goal, a.schedule = a.saved_goal, None, None
                else:
                    self.on_arrive(a)
                continue
            r = self.step_cooperative(a) if self.coop else self.step_independent(a)
            if r == "wait":
                return "wait" if a.job or a.stage in ("TO_HOME", "RETURN") else "idle"
        return "wait"

    def tick(self):
        for d in self.disturbances:
            d.on_tick(self)
        self.release_jobs()
        self.dispatch()
        self.max_queue = max(self.max_queue, sum(t.kind == "IN" for t in self.queue))
        self.max_tasks = max(self.max_tasks, len(self.queue))
        for a in self.amrs:     # CASE1: 작업장에 복귀했는데 줄 작업이 없으면 입고장을 비우고 대기장으로
            if a.stage == "AT_BASE" and a.job is None and a.base_since < self.now:
                self.set_goal(a, a.home, "TO_HOME")
        for a in self.amrs:                 # AMR1 → AMR2 → AMR3 순서 = 고정 우선순위
            cat = a.category = self.step(a)
            st = a.stats
            setattr(st, cat if cat != "tag" else "tag_wait", getattr(st, cat if cat != "tag" else "tag_wait") + 1)
            if a.rec is not None:
                if cat == "move":
                    a.rec.move += 1
                elif cat == "wait":
                    a.rec.wait += 1
                elif cat == "work":
                    a.rec.work += 1
                elif cat == "human":
                    a.rec.human += 1
        self.detector.update(self)
        self.now += 1
        if self.coop and self.now % 600 == 0:
            self.planner.table.purge(self.now - 1)

    @property
    def finished(self) -> bool:
        return self.done >= len(self.jobs)

    def run(self, max_ticks: int = 10 ** 7) -> SimResult:
        while not self.finished:
            self.tick()
            if self.now > max_ticks:
                raise RuntimeError(f"{self.name}: 시뮬레이션이 끝나지 않음 (교착 의심)")
        return self.result()

    # ------------------------------------------------------------ 출력
    def frame(self) -> str:
        pos = {a.cell: str(a.idx + 1) for a in self.amrs}
        base = render_ascii(self.grid).splitlines()
        out = base[:2]
        for line in base[2:]:
            y = int(line[2:4])
            row = list(line[5:])
            for (x, yy), ch in pos.items():
                if yy == y:
                    row[x] = ch
            out.append(line[:5] + "".join(row))
        out.append(f"[{self.name}] t={self.now * P.TICK:8.1f}s  " + "  ".join(a.status() for a in self.amrs))
        out.append(f"Queue: {len(self.queue)}   Completed: {self.done}/{len(self.jobs)}")
        return "\n".join(out)

    def result(self) -> SimResult:
        episodes = self.detector.finish()
        # 주문별 문제 카운트 귀속
        for ep in episodes:
            for oid in ep.orders:
                rec = self.records[int(oid[1:])]
                rec.problems[ep.kind] = rec.problems.get(ep.kind, 0) + 1
                rec.problem_ticks[ep.kind] = rec.problem_ticks.get(ep.kind, 0) + ep.duration
        snap = self.inventory.snapshot_accuracy()
        extra = {"종료 시점 재고 정확도(%)": snap[2]}
        for d in self.disturbances:
            extra.update(d.report())
        return SimResult(
            name=self.name, dispatcher=self.dispatcher.name, planner=self.planner.name,
            orders=sorted(self.records.values(), key=lambda r: r.job_id),
            amrs=[a.stats for a in self.amrs], end_tick=self.now, max_queue=self.max_queue,
            episodes=episodes,
            avoided_conflicts=getattr(self.planner, "predicted_conflicts", 0),
            mismatch_replans=self.mismatch_replans,
            max_tasks=self.max_tasks, chaining=self.chaining,
            collisions=list(self.collisions), human_stops=P.HUMAN_RESOLVE_STOPS,
            rfid=dict(self.rfid), rfid_interventions=self.rfid_interventions,
            worker_ticks=dict(self.worker_ticks),
            inventory=self.inventory.accuracy(),
            extra=extra,
        )
