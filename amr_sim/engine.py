"""AMR 3대 창고 시뮬레이션 엔진 (CASE1 · CASE2 공통).

시간 전진형: 1 tick = 0.1초. 각 AMR 는 칸 단위로 상하좌우 이동(대각선 없음)하며 한 칸은 한 대만 점유한다.
좁은 통로는 구역 전체를 한 번에 1대만 점유한다. 두 CASE 는 창고·주문·작업시간·RFID 결과·작업자 수가
모두 같고, 아래 두 부품만 다르다.
  - dispatcher : 작업배정·순서 (CASE1 단순 순차 / CASE2 OR-Tools VRP)
  - planner    : 경로 실행 (CASE1 독립 A* + 충돌 시 정지·대기 / CASE2 Cooperative A* + 예약테이블)

주문 1건 흐름 (stage)
  TO_IN → AT_IN(태그 대기·적재) → TO_RFID → AT_RFID(인식, 실패 시 작업자 개입) → TO_RACK
  → AT_RACK(적치+피킹+WMS) → TO_OUT(중앙교차로·좁은 통로) → AT_OUT(하역+WMS) → 완료 → TO_HOME

CASE1 충돌 처리: 다음 칸이 점유돼 있으면 정지·대기 (같은 tick 동시 요청은 AMR1>AMR2>AMR3 순).
  대기 후 출발 시 재출발 지연 1초. 막은 AMR 가 멈춰 있으면 2초 후 다른 AMR 칸을 피해 재계획,
  마주 보고 10초 이상 막히면 옆 칸으로 비켜섬 (교착 해소 → 1000건 완주 보장).
CASE2: 예약테이블에 따라 계획된 시각에 출발. 목적지가 작업 중이면 작업 종료 예상 시각에 맞춰 도착.

시간 분류 (AMR별, 매 tick 하나 → 합계 = 전체 시간)
  move 이동 / work 작업(적재·인식·적치·피킹·하역·WMS·작업자 개입) / wait 대기(다른 AMR·병목 때문에 멈춤,
  재출발 지연 포함) / tag 태그 부착 대기 / idle 유휴(배정된 주문 없음) / down 고장(방해요소)
"""
from __future__ import annotations

from collections import deque

from . import params as P
from .flow import remaining_ticks, service_ticks
from .jobs import Inventory, Order
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


class AMR:
    def __init__(self, idx: int, home):
        self.idx = idx
        self.name = f"AMR{idx + 1}"
        self.cell = self.home = home
        self.job: Order | None = None
        self.rec: OrderRecord | None = None
        self.stage = "IDLE"
        self.goal = None
        self.rack_cell = None
        self.slots = (None, None)
        self.path: list | None = None          # CASE1 남은 칸
        self.schedule: deque | None = None     # CASE2 [(출발 tick, 칸, 이동 tick)]
        self.retry_at = 0
        self.saved_goal = None                 # CASE2 비켜서기 중 원래 목적지
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
        j = self.job.order_id if self.job else "-"
        if self.category == "down":
            s = "DOWN"
        elif self.waiting or self.restart_left > 0:
            s = "WAIT"
        elif self.tasks:
            s = self.tasks[0][0]
        elif self.stage == "AT_IN":
            s = "TAG"
        else:
            s = {"IDLE": "IDLE", "TO_HOME": "HOME"}.get(self.stage, "MOVE")
        return f"{self.name}:{j}/{s}"


class Simulation:
    def __init__(self, name: str, orders: list[Order], dispatcher, planner, n_amr: int = P.AMR_COUNT,
                 grid: Grid | None = None, disturbances=(), seed: int = P.SEED):
        self.name = name
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
        self.detector = ProblemDetector()
        self.queue: list[Order] = []
        self.records: dict[int, OrderRecord] = {}
        self.tag_ready: dict[int, int] = {}
        self.tagger_free = 0
        self.now = 0
        self.next_job = 0
        self.done = 0
        self.dirty = False
        self.last_dispatch = -10 ** 9
        # 시스템 집계
        self.max_queue = 0
        self.mismatch_replans = 0     # CASE2: 예약 어긋남 → 재계획
        self.rfid = {"OK": 0, "FAIL": 0, "MISREAD": 0}
        self.worker_ticks = {"태그부착": 0, "RFID 오류": 0, "수동 배차 확인": 0}
        self.rfid_interventions = 0

    # ------------------------------------------------------------ 주문 발생
    def release_jobs(self):
        while self.next_job < len(self.jobs) and T(self.jobs[self.next_job].release) <= self.now:
            o = self.jobs[self.next_job]
            self.records[o.job_id] = OrderRecord(o.job_id, o.order_id, o.rack, T(o.release))
            tag = T(o.t_tag)            # 입고장 작업자가 도착순으로 RFID 태그 부착
            self.tagger_free = max(self.tagger_free, T(o.release)) + tag
            self.tag_ready[o.job_id] = self.tagger_free
            self.worker_ticks["태그부착"] += tag
            self.queue.append(o)
            self.next_job += 1
            self.dirty = True

    # ------------------------------------------------------------ 배정
    def operational(self, a: AMR) -> bool:
        return all(d.amr_available(self, a) for d in self.disturbances)

    def projected_state(self, a: AMR):
        """배정 계산용: AMR 가 현재 주문을 끝낼 위치와 예상 tick (자유주행 기준)."""
        if a.job is None:
            return a.pos, self.now
        return OUTBOUND_STATION, self.now + remaining_ticks(a)

    def order_cost(self, a: AMR, order: Order) -> tuple[int, int]:
        """(예상 수행비용 tick, 입고장까지 거리 m): 지금 이 AMR 에 주문을 주면 완료까지 걸릴 예상시간."""
        pos, free = self.projected_state(a)
        return (free - self.now) + path_ticks(pos, INBOUND_STATION, False) + service_ticks(order), \
            distance(pos, INBOUND_STATION)

    def dispatch(self):
        available = [a for a in self.amrs if a.job is None and self.operational(a)]
        if not (self.queue and available):
            return
        if not self.dirty and self.now - self.last_dispatch < REDISPATCH_EVERY:
            return
        self.dirty = False
        self.last_dispatch = self.now
        for job, a in self.dispatcher.assign(self, self.now, list(self.queue), available):
            self.queue.remove(job)
            self.start_job(a, job)

    def start_job(self, a: AMR, job: Order):
        # 배차 비효율 판정: 선택 가능 AMR(운용 중 전부, 바쁜 AMR 는 끝나는 시각 기준) 중 최소 비용과 비교
        costs = {b.name: self.order_cost(b, job) for b in self.amrs if self.operational(b)}
        rec = self.records[job.job_id]
        rec.assign = self.now
        rec.amr = a.name
        rec.cost, rec.assign_dist = costs[a.name]
        best = min(costs, key=lambda k: costs[k][0])
        rec.min_cost, best_dist = costs[best]
        rec.best_amr = best
        if rec.cost > rec.min_cost:
            rec.inefficient = True
            rec.extra_time = rec.cost - rec.min_cost
            rec.extra_dist = max(0, rec.assign_dist - best_dist)
            if rec.cost >= rec.min_cost * (1 + P.DISPATCH_CHECK_RATIO):
                rec.manual_check = True
                self.worker_ticks["수동 배차 확인"] += T(P.DISPATCH_CHECK_SEC)
        a.job, a.rec = job, rec
        a.rack_cell, a.slots = None, (None, None)
        self.set_goal(a, INBOUND_STATION, "TO_IN")

    def set_goal(self, a: AMR, goal, stage: str):
        a.goal, a.stage, a.path, a.schedule, a.saved_goal = goal, stage, None, None, None

    # ------------------------------------------------------------ 주문 흐름
    def run_tasks(self, a: AMR, stage: str, tasks, after):
        a.stage = stage
        a.tasks.extend([list(t) for t in tasks])
        a.after_tasks = after

    def on_arrive(self, a: AMR):
        o, rec = a.job, a.rec
        if a.stage == "TO_HOME":
            a.stage, a.goal = "IDLE", None
        elif a.stage == "TO_IN":
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
            tasks = [("PLACE", T(o.t_place)), ("PICK", T(o.t_pick)), ("WMS", T(P.RFID_WMS))]
            for d in self.disturbances:
                tasks += d.rack_tasks(self, a, o)
            self.run_tasks(a, "AT_RACK", tasks, self.after_rack)
        elif a.stage == "TO_OUT":
            self.run_tasks(a, "AT_OUT", [("UNLOAD", T(o.t_unload)), ("WMS", T(P.RFID_WMS))], self.complete_job)

    def after_load(self, a: AMR):
        a.loaded = True
        self.set_goal(a, RFID_GATE, "TO_RFID")

    def after_rfid(self, a: AMR):
        a.rec.t_rfid = self.now
        place, pick = self.inventory.choose_slots(a.job.rack)
        a.slots = (place, pick)
        slot = pick or place
        a.rack_cell = RACKS[a.job.rack].slot_access(int(slot.split("-")[1])) if slot else a.job.rack_cell
        self.set_goal(a, a.rack_cell, "TO_RACK")

    def after_rack(self, a: AMR):
        rec = a.rec
        rec.t_rack = self.now
        rec.tote_in, rec.tote_out = self.inventory.process(a.job, *a.slots)
        rec.slot_in, rec.slot_out = a.slots
        self.set_goal(a, OUTBOUND_STATION, "TO_OUT")

    def complete_job(self, a: AMR):
        a.rec.complete = self.now
        a.stats.jobs += 1
        self.done += 1
        self.dirty = True
        for d in self.disturbances:
            d.on_order_done(self, a, a.job)
        a.job, a.rec, a.loaded = None, None, False
        self.set_goal(a, a.home, "TO_HOME")

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
            rem = max(0, self.tag_ready[h.job.job_id] - self.now) + T(h.job.t_load)
            return self.now + rem + P.RESTART_TICKS
        return None

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
                    self.run_tasks(a, "AT_IN", [("LOAD", T(a.job.t_load))], self.after_load)
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
                return "wait" if a.job or a.stage == "TO_HOME" else "idle"
        return "wait"

    def tick(self):
        for d in self.disturbances:
            d.on_tick(self)
        self.release_jobs()
        self.dispatch()
        self.max_queue = max(self.max_queue, len(self.queue))
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
            rfid=dict(self.rfid), rfid_interventions=self.rfid_interventions,
            worker_ticks=dict(self.worker_ticks),
            inventory=self.inventory.accuracy(),
            extra=extra,
        )
