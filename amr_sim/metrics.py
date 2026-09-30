"""지표 기록: A. 주문별 로그 / B. AMR별 요약 / C. 시스템 요약 (시간 단위 tick → 초 변환)."""
from __future__ import annotations

import csv
from dataclasses import dataclass, field

from . import params as P


def sec(t: int | float) -> float:
    return t * P.TICK


@dataclass
class OrderRecord:
    job_id: int
    kind: str
    rack: str
    release: int
    assign: int = 0
    complete: int = 0
    amr: str = ""
    assign_dist: int = 0     # 배정 당시 AMR 위치 → 주문 출발지 A* 거리(m)
    distance: int = 0        # 이 주문 처리 중 이동거리(m)
    move: int = 0            # 이동 tick
    wait: int = 0            # 다른 AMR 때문에 멈춘 tick

    @property
    def lead(self) -> int:
        return self.complete - self.release

    @property
    def queue_wait(self) -> int:
        return self.assign - self.release


@dataclass
class AMRStats:
    name: str
    distance: int = 0
    move: int = 0
    work: int = 0
    wait: int = 0            # 대기: 다른 AMR/병목 때문에 멈춘 시간
    idle: int = 0            # 유휴: 배정된 작업 없이 서 있는 시간
    tag_wait: int = 0
    stops: int = 0
    jobs: int = 0


@dataclass
class SimResult:
    name: str
    dispatcher: str
    planner: str
    orders: list[OrderRecord]
    amrs: list[AMRStats]
    end_tick: int
    max_queue: int
    predicted_conflicts: int
    avoid_stops: int
    violations: int
    replans: int
    zone_wait_ticks: int
    zone_events: int
    zone_max_waiting: int
    rfid_ok: int
    rfid_fail: int
    interventions: int
    worker_ticks: int

    @property
    def makespan(self) -> float:
        return sec(max(o.complete for o in self.orders))

    @property
    def throughput(self) -> float:
        return len(self.orders) / (self.makespan / 3600)

    def avg(self, attr) -> float:
        return sum(sec(getattr(o, attr)) for o in self.orders) / len(self.orders)

    def system_summary(self) -> dict:
        return {
            "CASE": self.name,
            "배정": self.dispatcher,
            "경로": self.planner,
            "완료시간(makespan,s)": self.makespan,
            "시간당 처리량(건/h)": self.throughput,
            "평균 리드타임(s)": self.avg("lead"),
            "평균 배정대기(s)": self.avg("queue_wait"),
            "최대 대기 주문 수": self.max_queue,
            "총 이동거리(m)": sum(a.distance for a in self.amrs),
            "총 이동시간(s)": sec(sum(a.move for a in self.amrs)),
            "총 대기시간(s)": sec(sum(a.wait for a in self.amrs)),
            "총 유휴시간(s)": sec(sum(a.idle for a in self.amrs)),
            "충돌 예상 횟수": self.predicted_conflicts,
            "충돌 회피 정지 횟수": self.avoid_stops,
            "실제 충돌(같은 칸 동시 점유)": self.violations,
            "예약 어긋남 → 재계획 횟수": self.replans,
            "병목구간 대기시간(s)": sec(self.zone_wait_ticks),
            "병목구간 대기 발생 횟수": self.zone_events,
            "병목구간 최대 동시 대기 AMR 수": self.zone_max_waiting,
            "RFID 인식 성공": self.rfid_ok,
            "RFID 인식 실패": self.rfid_fail,
            "작업자 개입 횟수": self.interventions,
            "작업자 투입시간(s)": sec(self.worker_ticks),
        }

    def amr_rows(self) -> list[dict]:
        ms = self.makespan
        return [{
            "AMR": a.name, "처리건수": a.jobs, "이동거리(m)": a.distance, "이동시간(s)": sec(a.move),
            "작업시간(s)": sec(a.work), "대기시간(s)": sec(a.wait), "유휴시간(s)": sec(a.idle),
            "태그대기(s)": sec(a.tag_wait), "정지 횟수": a.stops,
            "시간당 처리(건/h)": a.jobs / (ms / 3600),
        } for a in self.amrs]


def _fmt(v):
    return f"{v:.1f}" if isinstance(v, float) else v


def save_order_log(res: SimResult, path: str):
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["주문ID", "종류", "랙", "발생시각(s)", "배정시각(s)", "완료시각(s)", "리드타임(s)",
                    "배정 AMR", "배정 당시 AMR-작업 거리(m)", "이동거리(m)", "이동시간(s)", "대기시간(s)"])
        for o in res.orders:
            w.writerow([o.job_id, o.kind, o.rack, _fmt(sec(o.release)), _fmt(sec(o.assign)), _fmt(sec(o.complete)),
                        _fmt(sec(o.lead)), o.amr, o.assign_dist, o.distance, _fmt(sec(o.move)), _fmt(sec(o.wait))])


def save_amr_summary(res: SimResult, path: str):
    rows = res.amr_rows()
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(rows[0].keys())
        for r in rows:
            w.writerow([_fmt(v) for v in r.values()])


def save_system_summary(results: list[SimResult], path: str):
    rows = [r.system_summary() for r in results]
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(rows[0].keys())
        for r in rows:
            w.writerow([_fmt(v) for v in r.values()])
