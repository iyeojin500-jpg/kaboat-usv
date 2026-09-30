"""AMR 3대 창고 시뮬레이션 엔진 (CASE1 · CASE2 공통).

시간 전진형: 1 tick = 0.1초. 각 AMR 는 칸 단위로 이동하며 한 칸은 한 번에 한 대만 점유한다.
두 CASE 는 창고·주문·작업시간·RFID 결과·작업자 수가 모두 같고, 아래 두 부품만 다르다.
  - dispatcher : 작업배정·순서 (CASE1 단순 순차 / CASE2 OR-Tools VRP)
  - planner    : 경로 실행 (CASE1 독립 A* + 충돌 시 정지·대기 / CASE2 Cooperative A* + 예약테이블)

CASE1 충돌 처리: 다음 칸이 점유돼 있으면 정지·대기 (같은 tick 동시 요청은 AMR1>AMR2>AMR3 순).
  대기 후 출발 시 재출발 지연 1초. 막은 AMR 가 멈춰 있으면 2초 후 다른 AMR 칸을 피해 재계획,
  마주 보고 10초 이상 막히면 옆 칸으로 비켜섬 (교착 해소 → 1000건 완주 보장).
CASE2: 예약테이블에 따라 계획된 시각에 출발. 계획상 대기(충돌 회피 정지)와 재출발 지연을 그대로 따른다.

시간 분류 (AMR별, 매 tick 하나로 분류 → 합계 = 전체 시간):
  이동 = 칸 사이 주행 중 (복귀 주행 포함)
  작업 = 적재·피킹·적치·하역·RFID·WMS·작업자 개입 대기
  대기 = 다른 AMR 때문에 멈춘 시간 (충돌 대기, 예약 대기, 재출발 지연, 경로 재시도)
  유휴 = 배정된 작업 없이 서 있는 시간
  태그대기 = 입고장에서 RFID 태그 부착이 끝나기를 기다린 시간
"""
from __future__ import annotations

from collections import deque

from . import params as P
from .jobs import Job
from .layout import AMR_HOMES, BOTTLENECK_ZONE, Grid, build_grid, render_ascii
from .metrics import AMRStats, OrderRecord, SimResult
from .motion import path_ticks, traverse_ticks
from .pathfinding import distance

REPLAN_AFTER = 20        # tick (CASE1 막힘 후 재계획)
SIDESTEP_AFTER = 100     # tick (CASE1 마주 보고 막힘 → 비켜서기)
PLAN_RETRY = 10          # tick (CASE2 경로 없음 → 재시도)
REDISPATCH_EVERY = 50    # tick (대기 주문 + 빈 AMR 가 있을 때 재배정 주기)


class AMR:
    def __init__(self, idx: int, home):
        self.idx = idx
        self.name = f"AMR{idx + 1}"
        self.cell = self.home = home
        self.job: Job | None = None
        self.rec: OrderRecord | None = None
        self.phase = "IDLE"          # IDLE, TO_PICKUP, WAIT_TAG, WORK, TO_DROP, TO_HOME
        self.goal = None
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
        self.blocked_by: AMR | None = None
        self.last_replan = 0
        self.restart_left = 0
        self.stats = AMRStats(self.name)

    @property
    def pos(self):
        return self.next_cell or self.cell

    def status(self) -> str:
        j = f"JOB{self.job.job_id}" if self.job else "-"
        if self.waiting or self.restart_left > 0:
            s = "WAIT"
        elif self.tasks:
            s = self.tasks[0][0]
        elif self.phase == "WAIT_TAG":
            s = "TAG"
        elif self.phase == "IDLE":
            s = "IDLE"
        elif self.phase == "TO_HOME":
            s = "HOME"
        else:
            s = "MOVE"
        return f"{self.name}:{j}/{s}"


class Simulation:
    def __init__(self, name: str, jobs: list[Job], dispatcher, planner, n_amr: int = P.AMR_COUNT,
                 grid: Grid | None = None):
        self.name = name
        self.jobs = sorted(jobs, key=lambda j: (j.release, j.job_id))
        self.grid = grid or build_grid()
        self.dispatcher = dispatcher
        self.planner = planner
        self.coop = getattr(planner, "cooperative", False)
        self.amrs = [AMR(i, AMR_HOMES[i]) for i in range(n_amr)]
        self.occupied = {a.cell: a for a in self.amrs}
        if self.coop:
            for a in self.amrs:
                planner.park(a.idx, a.cell, 0)
        self.queue: list[Job] = []
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
        self.conflicts = 0            # CASE1: 다음 칸이 점유돼 멈춘 횟수 (= 충돌 발생)
        self.violations = 0           # CASE2: 경로 중간 칸 예약 어긋남 → 재계획
        self.late_release = 0         # CASE2: 목적지 점유 AMR 이탈 지연 → 도착 직전 재계획
        self.zone_wait = 0            # 병목구간 대기 tick
        self.zone_events = 0
        self.zone_max_waiting = 0
        self.rfid_ok = 0
        self.rfid_fail = 0
        self.interventions = 0
        self.worker_ticks = 0

    # ------------------------------------------------------------ 주문 발생
    def release_jobs(self):
        while self.next_job < len(self.jobs) and P.ticks(self.jobs[self.next_job].release) <= self.now:
            job = self.jobs[self.next_job]
            self.records[job.job_id] = OrderRecord(job.job_id, job.kind, job.rack, P.ticks(job.release))
            if job.kind == "INBOUND":   # 입고장 작업자가 도착순으로 태그 부착
                tag = P.ticks(job.t_tag)
                self.tagger_free = max(self.tagger_free, P.ticks(job.release)) + tag
                self.tag_ready[job.job_id] = self.tagger_free
                self.worker_ticks += tag
            self.queue.append(job)
            self.next_job += 1
            self.dirty = True

    # ------------------------------------------------------------ 배정
    def projected_state(self, a: AMR):
        """배정 계산용: AMR 가 현재 작업을 끝낼 위치와 예상 tick (자유주행 기준)."""
        if a.job is None:
            return a.pos, self.now
        job = a.job
        rem = sum(t[1] for t in a.tasks)
        t2 = P.ticks(job.t_second) + P.ticks(P.RFID_READ) + 2 * P.ticks(P.RFID_WMS)
        if a.phase in ("TO_PICKUP", "WAIT_TAG"):
            rem += (path_ticks(a.pos, job.pickup_cell, False) + P.ticks(job.t_first)
                    + path_ticks(job.pickup_cell, job.drop_cell, True) + t2)
        elif a.phase == "TO_DROP":
            rem += path_ticks(a.pos, job.drop_cell, True) + t2
        elif a.phase == "WORK" and not a.loaded:
            rem += path_ticks(job.pickup_cell, job.drop_cell, True) + t2
        return job.drop_cell, self.now + rem

    def dispatch(self):
        available = [a for a in self.amrs if a.job is None]
        if not (self.queue and available):
            return
        if not self.dirty and self.now - self.last_dispatch < REDISPATCH_EVERY:
            return
        self.dirty = False
        self.last_dispatch = self.now
        for job, a in self.dispatcher.assign(self, self.now, list(self.queue), available):
            self.queue.remove(job)
            self.start_job(a, job)

    def start_job(self, a: AMR, job: Job):
        a.job = job
        a.rec = rec = self.records[job.job_id]
        rec.assign = self.now
        rec.amr = a.name
        rec.assign_dist = distance(a.pos, job.pickup_cell)
        self.set_goal(a, job.pickup_cell, "TO_PICKUP")

    def set_goal(self, a: AMR, goal, phase: str):
        a.goal, a.phase, a.path, a.schedule, a.saved_goal = goal, phase, None, None, None

    # ------------------------------------------------------------ 작업 흐름
    def rfid_tasks(self, a: AMR):
        tasks = [("RFID", P.ticks(P.RFID_READ))]
        if a.job.rfid_fail:
            self.rfid_fail += 1
            self.interventions += 1
            self.worker_ticks += P.ticks(P.WORKER_INTERVENTION)
            tasks.append(("HELP", P.ticks(P.WORKER_INTERVENTION)))
        else:
            self.rfid_ok += 1
        return tasks

    def run_tasks(self, a: AMR, tasks, after):
        a.phase = "WORK"
        a.tasks.extend([list(t) for t in tasks])
        a.after_tasks = after

    def on_arrive(self, a: AMR):
        job = a.job
        if a.phase == "TO_HOME":
            a.phase, a.goal = "IDLE", None
        elif a.phase == "TO_PICKUP":
            if job.kind == "INBOUND":
                a.phase = "WAIT_TAG"
            else:
                self.run_tasks(a, [("PICK", P.ticks(job.t_first))], self.after_pickup)
        elif a.phase == "TO_DROP":
            if job.kind == "INBOUND":
                tasks = [("PLACE", P.ticks(job.t_second)), ("WMS", P.ticks(P.RFID_WMS))]
            else:
                tasks = ([("UNLOAD", P.ticks(job.t_second))] + self.rfid_tasks(a)
                         + [("WMS", P.ticks(P.RFID_WMS))])
            self.run_tasks(a, tasks, self.complete_job)

    def after_pickup(self, a: AMR):
        a.loaded = True
        self.set_goal(a, a.job.drop_cell, "TO_DROP")

    def complete_job(self, a: AMR):
        a.rec.complete = self.now
        a.stats.jobs += 1
        self.done += 1
        self.dirty = True
        a.job, a.rec, a.loaded = None, None, False
        self.set_goal(a, a.home, "TO_HOME")

    # ------------------------------------------------------------ 대기 표시
    def mark_wait(self, a: AMR):
        if not a.waiting:
            a.waiting = True
            a.wait_start = self.now
            a.stats.stops += 1
            if a.cell in BOTTLENECK_ZONE:
                self.zone_events += 1
        return "wait"

    def begin_move(self, a: AMR, nxt, dur: int):
        self.occupied[nxt] = a
        a.next_cell = nxt
        a.move_left = a.move_dur = dur
        a.waiting = False
        a.blocked_by = None

    # ------------------------------------------------------------ CASE1: 독립 A*
    def step_independent(self, a: AMR) -> str:
        if not a.path:
            a.path = self.planner.plan(a.cell, a.goal)
        nxt = a.path[0]
        owner = self.occupied.get(nxt)
        if owner is None:
            a.path.pop(0)
            was_waiting = a.waiting
            self.begin_move(a, nxt, traverse_ticks(a.cell, nxt, a.heading, a.loaded))
            if was_waiting:
                a.restart_left = P.RESTART_TICKS
            return "go"
        if not a.waiting:
            self.conflicts += 1
            a.last_replan = self.now
        self.mark_wait(a)
        a.blocked_by = owner
        if owner.next_cell is None and self.now - a.last_replan >= REPLAN_AFTER:
            a.last_replan = self.now
            avoid = {c for c, o in self.occupied.items() if o is not a}
            p = self.planner.plan(a.cell, a.goal, avoid)
            if p and p[0] not in self.occupied:
                a.path = p
                return "retry"
            if (self.now - a.wait_start >= SIDESTEP_AFTER and owner.waiting and owner.blocked_by is a):
                owner_next = set(owner.path[:2]) if owner.path else set()
                for n in self.grid.neighbors(a.cell):
                    if n not in self.occupied and n not in owner_next:
                        a.path = [n]
                        return "retry"
        return "wait"

    # ------------------------------------------------------------ CASE2: 예약테이블
    def step_cooperative(self, a: AMR) -> str:
        if not a.schedule:
            if self.now < a.retry_at:
                return self.mark_wait(a)
            moves = self.planner.plan_timed(a.idx, a.cell, a.goal, self.now, a.heading, a.loaded,
                                            goal_release=self.expected_release(a.goal))
            if moves is None:
                a.retry_at = self.now + PLAN_RETRY
                if self.in_goal_cycle(a):
                    self.step_aside(a)
                return self.mark_wait(a)
            a.schedule = deque(moves)
        dep, nxt, dur = a.schedule[0]
        if self.now < dep:
            return self.mark_wait(a)
        owner = self.occupied.get(nxt)
        if owner is not None or self.now > dep:
            # 앞 AMR 가 계획보다 늦게 떠나 예약이 어긋남 → 멈추고 재계획 (물리적 충돌은 엔진이 막음)
            if nxt == a.goal and owner is not None:
                self.late_release += 1
            else:
                self.violations += 1
            # 목적지 점유 AMR 가 예상보다 늦게 떠나는 경우 → 옆에서 기다렸다가 재계획
            a.schedule = None
            return self.mark_wait(a)
        a.schedule.popleft()
        self.begin_move(a, nxt, dur)
        return "go"

    def expected_release(self, cell):
        """cell 을 차지하고 작업 중인 AMR 가 떠날 것으로 예상되는 tick (모르면 None)."""
        h = self.occupied.get(cell)
        if h is None or h.next_cell is not None:
            return None
        if h.tasks:
            rem = sum(t[1] for t in h.tasks)
            return self.now + rem + P.RESTART_TICKS
        if h.phase == "WAIT_TAG":
            rem = max(0, self.tag_ready[h.job.job_id] - self.now) + P.ticks(h.job.t_first) + P.ticks(P.RFID_READ)
            return self.now + rem + P.RESTART_TICKS
        return None

    def in_goal_cycle(self, a: AMR) -> bool:
        """a 의 목적지를 점유한 AMR → 그 AMR 의 목적지를 점유한 AMR → ... 가 a 로 돌아오면 교착."""
        cur, seen = a, set()
        while cur is not None and cur.idx not in seen:
            seen.add(cur.idx)
            holder = self.occupied.get(cur.goal) if cur.goal else None
            if holder is None or holder is cur or holder.tasks or holder.phase == "WAIT_TAG" or holder.schedule:
                return False
            if holder is a:
                return True
            cur = holder
        return False

    def step_aside(self, a: AMR):
        """교착 해소: 다른 AMR 목적지·경로와 겹치지 않는 가장 가까운 빈 칸으로 잠시 비켜선다."""
        from .pathfinding import static_path
        banned = set()
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
                    if n not in banned and n not in self.occupied and not self.planner.table.held_forever_by_other(n, a.idx):
                        a.saved_goal = a.saved_goal or a.goal
                        a.goal, a.schedule, a.retry_at = n, None, self.now
                        self.planner.asides += 1
                        return
                    nxt.append(n)
            frontier = nxt

    # ------------------------------------------------------------ 한 tick
    def step(self, a: AMR) -> str:
        for _ in range(20):
            if a.tasks:
                task = a.tasks[0]
                task[1] -= 1
                if task[1] <= 0:
                    a.tasks.popleft()
                    if not a.tasks:
                        a.after_tasks(a)
                return "work"
            if a.phase == "WAIT_TAG":
                if self.now >= self.tag_ready[a.job.job_id]:
                    self.run_tasks(a, [("LOAD", P.ticks(a.job.t_first))] + self.rfid_tasks(a), self.after_pickup)
                    continue
                return "tag"
            if a.next_cell is not None:
                if a.restart_left > 0:
                    a.restart_left -= 1
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
                return "wait" if a.job or a.phase == "TO_HOME" else "idle"
        return "wait"

    def tick(self):
        self.release_jobs()
        self.dispatch()
        self.max_queue = max(self.max_queue, len(self.queue))
        zone_waiting = 0
        for a in self.amrs:                 # AMR1 → AMR2 → AMR3 순서 = 고정 우선순위
            cat = self.step(a)
            st = a.stats
            if cat == "move":
                st.move += 1
            elif cat == "work":
                st.work += 1
            elif cat == "wait":
                st.wait += 1
                if a.cell in BOTTLENECK_ZONE:
                    self.zone_wait += 1
                    zone_waiting += 1
            elif cat == "tag":
                st.tag_wait += 1
            else:
                st.idle += 1
            if a.rec is not None:
                if cat == "move":
                    a.rec.move += 1
                elif cat == "wait":
                    a.rec.wait += 1
        self.zone_max_waiting = max(self.zone_max_waiting, zone_waiting)
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
        planner = self.planner
        coop = self.coop
        return SimResult(
            name=self.name,
            dispatcher=self.dispatcher.name,
            planner=planner.name,
            orders=sorted(self.records.values(), key=lambda r: r.job_id),
            amrs=[a.stats for a in self.amrs],
            end_tick=self.now,
            max_queue=self.max_queue,
            predicted_conflicts=planner.predicted_conflicts if coop else self.conflicts,
            avoid_stops=sum(a.stats.stops for a in self.amrs),
            violations=0,     # 엔진이 칸 단독 점유를 강제하므로 물리적 충돌은 구조적으로 0
            replans=self.violations + self.late_release,
            zone_wait_ticks=self.zone_wait,
            zone_events=self.zone_events,
            zone_max_waiting=self.zone_max_waiting,
            rfid_ok=self.rfid_ok,
            rfid_fail=self.rfid_fail,
            interventions=self.interventions,
            worker_ticks=self.worker_ticks,
        )
