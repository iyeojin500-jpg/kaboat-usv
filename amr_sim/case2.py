"""CASE2 — 기본 AMR 자동화 (작업자 2명 + RFID + 로봇팔 AMR 3대 + WMS).

시간 전진형(Δt=0.1s) 시뮬레이션. 각 AMR 는 셀 단위로 이동하며, 셀은 한 번에 한 대만 점유한다.

- 작업배정: Dispatcher (기본: SimpleDispatcher — 비어있는 AMR 중 AMR1→2→3 순, 작업은 FIFO)
- 경로계산: PathPlanner (기본: IndependentPlanner — 각자 A*, 다른 AMR 미래 위치 무시)
- 충돌 처리: 다음 셀이 다른 AMR 에게 점유돼 있으면 정지 후 대기(병목 대기 타이머 시작).
  같은 틱에 같은 셀을 요청하면 우선순위 AMR1 > AMR2 > AMR3 (번호 순으로 먼저 처리).
  다시 출발할 때 재출발 지연 1초 (병목 대기시간에 포함).
- 교착 회피(기본 장애물 회피): 막은 AMR 가 멈춰 있고(작업중/대기중) 2초 이상 기다리면,
  현재 다른 AMR 가 서 있는 셀을 피해 경로를 다시 계산. 서로 마주 보고 막힌 상태가 10초 이상이면
  옆 칸으로 비켜선다.
- 작업자 2명: 입고장 1명(RFID 태그 부착), 출고장 1명(포장·확인) — 스테이션 고정, 이동 없음.

입고: 입고장 이동 → (태그 부착 완료 대기) → 로봇팔 적재 → RFID 인식 → 랙 이동 → 적치 → WMS 갱신
출고: 랙 이동 → 피킹 → 출고장 이동 → 하역 → RFID 확인 → WMS 갱신
"""
from __future__ import annotations

import random
from collections import deque

from . import params as P
from .jobs import Job
from .layout import AMR_HOMES, INTERSECTIONS, Grid, build_grid, render_ascii
from .metrics import CaseResult, JobRecord
from .strategies import Dispatcher, IndependentPlanner, PathPlanner, SimpleDispatcher

DT = 0.1
EPS = 1e-9
REPLAN_AFTER = 2.0
SIDESTEP_AFTER = 10.0


class AMR:
    def __init__(self, idx: int, home):
        self.idx = idx
        self.name = f"AMR{idx + 1}"
        self.cell = self.home = home
        self.job: Job | None = None
        self.rec: JobRecord | None = None
        self.phase = "IDLE"            # IDLE, TO_PICKUP, WAIT_TAG, TO_DROP, WORK, TO_HOME
        self.goal = None
        self.path: list | None = None
        self.next_cell = None
        self.move_left = 0.0
        self.heading = None
        self.loaded = False
        self.tasks: deque = deque()    # (종류, 남은시간, 분류 'handle'|'id')
        self.after_tasks = None
        self.blocked = False
        self.block_start = 0.0
        self.blocked_by: AMR | None = None
        self.last_replan = 0.0
        self.restart_left = 0.0
        # 통계
        self.distance = 0
        self.jobs_done = 0
        self.busy = 0.0
        self.block_total = 0.0
        self.block_events = 0

    def status(self) -> str:
        j = f"JOB{self.job.job_id}" if self.job else "-"
        if self.blocked or self.restart_left > EPS:
            s = "병목대기"
        elif self.tasks:
            s = {"LOAD": "적재중", "PICK": "피킹중", "PLACE": "적치중", "UNLOAD": "하역중"}.get(self.tasks[0][0], "RFID/WMS")
        elif self.phase == "WAIT_TAG":
            s = "태그대기"
        elif self.phase == "IDLE":
            s = "대기(주차)"
        else:
            s = "이동중" if self.phase != "TO_HOME" else "복귀중"
        return f"{self.name}: {j}/{s}"


class Case2Sim:
    def __init__(self, jobs: list[Job], seed: int = P.SEED, n_amr: int = P.AMR_COUNT,
                 dispatcher: Dispatcher | None = None, planner: PathPlanner | None = None,
                 grid: Grid | None = None):
        self.rng = random.Random(seed)
        self.jobs = sorted(jobs, key=lambda j: (j.release, j.job_id))
        self.grid = grid or build_grid()
        self.dispatcher = dispatcher or SimpleDispatcher()
        self.planner = planner or IndependentPlanner()
        self.amrs = [AMR(i, AMR_HOMES[i]) for i in range(n_amr)]
        self.occupied = {a.cell: a for a in self.amrs}
        self.queue: deque[Job] = deque()
        self.records: dict[int, JobRecord] = {}
        self.tag_ready: dict[int, float] = {}
        self.tag_rfid: dict[int, float] = {}
        self.tagger_free = 0.0
        self.t = 0.0
        self.done = 0
        self.max_queue = 0
        self.snapshots: list[str] = []

    # ------------------------------------------------------------ 이동
    def traverse_time(self, a: AMR, nxt) -> float:
        v = P.AMR_SPEED_LOADED if a.loaded else P.AMR_SPEED_EMPTY
        d = (nxt[0] - a.cell[0], nxt[1] - a.cell[1])
        if a.heading is not None and d != a.heading:
            v = min(v, P.AMR_SPEED_TURN)
        if nxt in INTERSECTIONS:
            v = min(v, P.AMR_SPEED_CROSS)
        return 1.0 / v

    def set_goal(self, a: AMR, goal, phase: str):
        a.goal, a.phase, a.path = goal, phase, None

    def plan(self, a: AMR, avoid=None):
        return self.planner.plan(self.grid, a.cell, a.goal, a, self.t, avoid)

    # ------------------------------------------------------------ 작업 흐름
    def start_job(self, a: AMR, job: Job, now: float):
        a.job = job
        a.rec = rec = self.records[job.job_id]
        rec.assign = rec.start = now
        rec.resource = a.name
        self.set_goal(a, job.pickup_cell, "TO_PICKUP")

    def on_arrive(self, a: AMR, now: float):
        job = a.job
        if a.phase == "TO_HOME":
            a.phase, a.goal = "IDLE", None
        elif a.phase == "TO_PICKUP":
            if job.kind == "INBOUND":
                a.phase = "WAIT_TAG"
            else:
                self.run_tasks(a, [("PICK", P.jitter(self.rng, P.AMR_PICK), "handle")], self.after_pickup)
        elif a.phase == "TO_DROP":
            if job.kind == "INBOUND":
                tasks = [("PLACE", P.jitter(self.rng, P.AMR_PLACE), "handle"), ("WMS", P.RFID_WMS, "id")]
            else:
                tasks = [("UNLOAD", P.jitter(self.rng, P.AMR_UNLOAD), "handle"),
                         ("RFID", P.RFID_READ, "id"), ("WMS", P.RFID_WMS, "id")]
            self.run_tasks(a, tasks, self.complete_job)

    def run_tasks(self, a: AMR, tasks, after):
        a.phase = "WORK"
        a.tasks.extend([list(t) for t in tasks])
        a.after_tasks = after

    def after_pickup(self, a: AMR, now: float):
        a.loaded = True
        self.set_goal(a, a.job.drop_cell, "TO_DROP")

    def complete_job(self, a: AMR, now: float):
        rec = a.rec
        rec.complete = now
        if a.job.kind == "INBOUND":
            rec.id_time += self.tag_rfid[a.job.job_id]   # 사람 태그 부착 시간
        a.busy += now - rec.assign
        a.jobs_done += 1
        self.done += 1
        a.job, a.rec, a.loaded = None, None, False
        self.set_goal(a, a.home, "TO_HOME")

    # ------------------------------------------------------------ 한 틱
    def end_block(self, a: AMR, now: float):
        dur = now - a.block_start
        a.blocked, a.blocked_by = False, None
        a.block_total += dur
        if a.rec:
            a.rec.block_time += dur
        a.restart_left = P.AMR_RESTART_DELAY

    def step(self, a: AMR, dt: float):
        budget = dt
        guard = 0
        while budget > EPS:
            guard += 1
            if guard > 50:
                break
            now = self.t + (dt - budget)
            # 1) 로봇팔/RFID 작업
            if a.tasks:
                task = a.tasks[0]
                use = min(budget, task[1])
                task[1] -= use
                budget -= use
                if task[2] == "handle":
                    a.rec.handling_time += use
                else:
                    a.rec.id_time += use
                if task[1] <= EPS:
                    a.tasks.popleft()
                    if not a.tasks:
                        a.after_tasks(a, self.t + (dt - budget))
                continue
            # 2) 입고장 태그 대기
            if a.phase == "WAIT_TAG":
                ready = self.tag_ready[a.job.job_id]
                if now + EPS >= ready:
                    self.run_tasks(a, [("LOAD", P.jitter(self.rng, P.AMR_LOAD), "handle"),
                                       ("RFID", P.RFID_READ, "id")], self.after_pickup)
                    continue
                use = min(budget, ready - now)
                a.rec.station_wait += use
                budget -= use
                continue
            # 3) 셀 사이 이동 중
            if a.next_cell is not None:
                if a.restart_left > EPS:
                    use = min(budget, a.restart_left)
                    a.restart_left -= use
                    budget -= use
                    a.block_total += use
                    if a.rec:
                        a.rec.block_time += use
                    continue
                use = min(budget, a.move_left)
                a.move_left -= use
                budget -= use
                if a.rec:
                    a.rec.move_time += use
                if a.move_left <= EPS:
                    del self.occupied[a.cell]
                    a.heading = (a.next_cell[0] - a.cell[0], a.next_cell[1] - a.cell[1])
                    a.cell, a.next_cell = a.next_cell, None
                    a.distance += 1
                    if a.rec:
                        a.rec.distance += 1
                continue
            # 4) 셀 위에 정지 — 다음 행동 결정
            if a.goal is None:
                break
            if a.cell == a.goal:
                self.on_arrive(a, now)
                continue
            if not a.path:
                a.path = self.plan(a)
            nxt = a.path[0]
            owner = self.occupied.get(nxt)
            if owner is None:
                self.occupied[nxt] = a
                a.path.pop(0)
                a.move_left = self.traverse_time(a, nxt)
                a.next_cell = nxt
                if a.blocked:
                    self.end_block(a, now)
                continue
            # 막힘
            if not a.blocked:
                a.blocked, a.block_start, a.last_replan = True, now, now
                a.block_events += 1
                if a.rec:
                    a.rec.block_events += 1
            a.blocked_by = owner
            if owner.next_cell is None and now - a.last_replan >= REPLAN_AFTER - EPS:
                a.last_replan = now
                avoid = {c for c, o in self.occupied.items() if o is not a}
                p = self.plan(a, avoid)
                if p and p[0] not in self.occupied:
                    a.path = p
                    continue
                if (now - a.block_start >= SIDESTEP_AFTER and owner.blocked and owner.blocked_by is a):
                    owner_next = set(owner.path[:2]) if owner.path else set()
                    for n in self.grid.neighbors(a.cell):
                        if n not in self.occupied and n not in owner_next:
                            a.path = [n]
                            break
                    else:
                        break
                    continue
            break

    # ------------------------------------------------------------ 메인 루프
    def release_jobs(self, idx: int) -> int:
        while idx < len(self.jobs) and self.jobs[idx].release <= self.t + EPS:
            job = self.jobs[idx]
            self.records[job.job_id] = JobRecord(job.job_id, job.kind, job.rack, job.release)
            if job.kind == "INBOUND":   # 입고장 작업자가 도착 순서대로 태그 부착
                tag = P.jitter(self.rng, P.RFID_TAG)
                self.tagger_free = max(self.tagger_free, job.release) + tag
                self.tag_ready[job.job_id] = self.tagger_free
                self.tag_rfid[job.job_id] = tag
            self.queue.append(job)
            idx += 1
        return idx

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
        out.append(f"t={self.t:8.1f}s   " + "   ".join(a.status() for a in self.amrs))
        out.append(f"Queue: {len(self.queue)}   Completed: {self.done}/{len(self.jobs)}")
        return "\n".join(out)

    def run(self, snapshot_times=(), max_time: float = 10 * 24 * 3600) -> CaseResult:
        idx = 0
        snaps = sorted(snapshot_times)
        n = len(self.jobs)
        while self.done < n:
            idx = self.release_jobs(idx)
            available = [a for a in self.amrs if a.job is None]
            if self.queue and available:
                for job, a in self.dispatcher.assign(self.t, self.queue, available):
                    self.queue.remove(job)
                    self.start_job(a, job, self.t)
            self.max_queue = max(self.max_queue, len(self.queue))
            for a in self.amrs:          # AMR1 → AMR2 → AMR3 순서 = 고정 우선순위
                self.step(a, DT)
            self.t = round(self.t + DT, 6)
            while snaps and snaps[0] <= self.t:
                snaps.pop(0)
                self.snapshots.append(self.frame())
            if self.t > max_time:
                raise RuntimeError("시뮬레이션이 끝나지 않음 (교착 의심)")
        return self.result()

    def result(self) -> CaseResult:
        records = list(self.records.values())
        res = CaseResult(
            name="CASE2",
            records=records,
            total_distance=sum(a.distance for a in self.amrs),
            worker_move_time=0.0,           # 작업자 2명은 스테이션 고정
            max_queue=self.max_queue,
            amr_block_events=sum(a.block_events for a in self.amrs),
            amr_block_time=sum(a.block_total for a in self.amrs),
        )
        ms = res.makespan
        res.resources = [{"자원": a.name, "처리건수": a.jobs_done, "이동거리(m)": a.distance,
                          "가동률(%)": 100 * a.busy / ms, "시간당 처리(건/h)": a.jobs_done / (ms / 3600),
                          "가동시간당 처리(건/h)": a.jobs_done / (a.busy / 3600) if a.busy else 0.0,
                          "병목정지횟수": a.block_events, "병목대기시간(s)": a.block_total}
                         for a in self.amrs]
        return res


def run_case2(jobs: list[Job], seed: int = P.SEED, snapshot_times=(), **kw) -> tuple[CaseResult, list[str]]:
    sim = Case2Sim(jobs, seed=seed, **kw)
    res = sim.run(snapshot_times)
    return res, sim.snapshots
