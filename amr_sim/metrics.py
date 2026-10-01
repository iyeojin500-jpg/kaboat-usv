"""측정값 기록·KPI 계산 — 주문별 / AMR별 / 시스템 3개 CSV (+ 문제 에피소드 로그).

시간 단위: 내부 tick(0.1초) → 출력은 초.
"""
from __future__ import annotations

import csv
from dataclasses import dataclass, field

from . import params as P
from .layout import ZONES
from .problems import summarize, zone_breakdown

KINDS = ("경로충돌", "정면충돌", "병목", "장시간정체")


def sec(t) -> float:
    return t * P.TICK


@dataclass
class OrderRecord:
    job_id: int
    order_id: str
    rack: str
    release: int
    assign: int = 0           # = 작업시작 (AMR 가 이 주문을 위해 움직이기 시작)
    t_in: int = 0             # 입고장 도착
    t_rfid: int = 0           # RFID 통과 완료
    t_rack: int = 0           # 저장구역 처리 완료
    complete: int = 0         # 출고 완료
    amr: str = ""
    cost: int = 0             # 배차 시 배정 AMR 의 예상 수행비용 (tick)
    min_cost: int = 0         # 그 시점 선택 가능 AMR 중 최소 예상 수행비용
    best_amr: str = ""
    assign_dist: int = 0      # 배정 시 AMR → 입고장 거리(m)
    inefficient: bool = False
    extra_time: int = 0
    extra_dist: int = 0
    manual_check: bool = False
    distance: int = 0
    move: int = 0
    work: int = 0
    wait: int = 0
    tote_in: str = ""
    tote_out: str | None = ""
    slot_in: str | None = ""
    slot_out: str | None = ""
    problems: dict = field(default_factory=dict)
    problem_ticks: dict = field(default_factory=dict)

    @property
    def lead(self) -> int:          # 총 처리시간 = 출고완료 − 주문발생
        return self.complete - self.release

    @property
    def response(self) -> int:      # 주문 대응시간 = 작업시작 − 주문발생
        return self.assign - self.release

    @property
    def execution(self) -> int:     # 실행시간 = 출고완료 − 작업시작 (대기열 시간 제외, 참고용)
        return self.complete - self.assign


@dataclass
class AMRStats:
    name: str
    distance: int = 0
    move: int = 0
    work: int = 0
    wait: int = 0             # 대기: 다른 AMR·병목 때문에 멈춘 시간 (재출발 지연 포함)
    tag_wait: int = 0         # 입고장 태그 부착 대기
    idle: int = 0             # 유휴: 배정된 주문 없이 서 있는 시간
    down: int = 0             # 고장 (방해요소)
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
    episodes: list
    avoided_conflicts: int
    mismatch_replans: int
    rfid: dict
    rfid_interventions: int
    worker_ticks: dict
    inventory: tuple
    extra: dict

    # ---------------------------------------------------------------- 기본량
    @property
    def makespan(self) -> float:
        return sec(max(o.complete for o in self.orders))

    def avg(self, attr) -> float:
        return sum(sec(getattr(o, attr)) for o in self.orders) / len(self.orders)

    def utilization(self, a: AMRStats) -> float:
        return 100.0 * (a.move + a.work) / (self.makespan / P.TICK)

    @property
    def stall(self) -> dict:
        return summarize(self.episodes, "장시간정체")

    @property
    def manual_dispatch(self) -> int:
        return sum(o.manual_check for o in self.orders)

    @property
    def human_interventions(self) -> int:
        """KPI '인력 의존도' 대상: 장시간 정체 개입 + 수동 배차 확인 개입."""
        return self.stall["횟수"] + self.manual_dispatch

    # ---------------------------------------------------------------- KPI 5종
    def kpis(self) -> dict:
        return {
            "작업 처리시간(평균, s)": self.avg("lead"),
            "설비 가동률(%)": sum(self.utilization(a) for a in self.amrs) / len(self.amrs),
            "재고 정확도(%)": self.inventory[2],
            "사람 개입 횟수": self.human_interventions,
            "주문 대응시간(평균, s)": self.avg("response"),
        }

    # ---------------------------------------------------------------- 시스템 요약
    def system_summary(self) -> dict:
        n = len(self.orders)
        ms = self.makespan
        d = {"CASE": self.name, "배정": self.dispatcher, "경로": self.planner,
             "완료시간(makespan,s)": ms, "시간당 처리량(건/h)": n / (ms / 3600),
             "평균 총처리시간(s)": self.avg("lead"), "평균 주문 대응시간(s)": self.avg("response"),
             "평균 실행시간(s)": self.avg("execution"),
             "최대 대기 주문 수": self.max_queue,
             "총 이동거리(m)": sum(a.distance for a in self.amrs),
             "총 이동시간(s)": sec(sum(a.move for a in self.amrs)),
             "총 작업시간(s)": sec(sum(a.work for a in self.amrs)),
             "총 대기시간(s)": sec(sum(a.wait for a in self.amrs)),
             "총 유휴시간(s)": sec(sum(a.idle for a in self.amrs)),
             "정지 횟수": sum(a.stops for a in self.amrs),
             "설비 가동률(%)": self.kpis()["설비 가동률(%)"]}
        for kind, label in (("경로충돌", "경로 충돌"), ("정면충돌", "정면 충돌"), ("병목", "병목")):
            s = summarize(self.episodes, kind)
            d[f"{label} 횟수"] = s["횟수"]
            d[f"{label} 대기시간(s)"] = s["총시간(s)"]
            d[f"{label} 평균 지속(s)"] = s["평균(s)"]
            d[f"{label} 최대 지속(s)"] = s["최대(s)"]
            d[f"{label} 관련 주문수"] = s["관련 주문수"]
        for z, s in zone_breakdown(self.episodes).items():
            d[f"병목[{z}] 횟수"] = s["횟수"]
            d[f"병목[{z}] 대기시간(s)"] = s["총시간(s)"]
        ineff = [o for o in self.orders if o.inefficient]
        d.update({
            "배차 비효율 횟수": len(ineff),
            "배차 비효율률(%)": 100.0 * len(ineff) / n,
            "배차 추가 이동거리(m)": sum(o.extra_dist for o in ineff),
            "배차 추가 예상시간(s)": sec(sum(o.extra_time for o in ineff)),
        })
        st = self.stall
        d.update({
            "장시간 정체 개입 횟수": st["횟수"],
            "장시간 정체 개입시간(s)": st["총시간(s)"],
            "장시간 정체 관련 주문수": st["관련 주문수"],
            "수동 배차 확인 개입 횟수": self.manual_dispatch,
            "수동 배차 확인 개입시간(s)": self.manual_dispatch * P.DISPATCH_CHECK_SEC,
            "수동 배차 확인 관련 주문수": self.manual_dispatch,
            "수동 배차 확인 / 주문 100건": 100.0 * self.manual_dispatch / n,
            "사람 개입 합계(정체+배차)": self.human_interventions,
            "RFID 인식 성공": self.rfid.get("OK", 0),
            "RFID 인식 실패(감지·작업자 처리)": self.rfid.get("FAIL", 0),
            "RFID 오인식(미감지)": self.rfid.get("MISREAD", 0),
            "RFID 오류 작업자 개입 횟수": self.rfid_interventions,
            "작업자 투입시간(s)": sec(sum(self.worker_ticks.values())),
            **{f"작업자 투입시간[{k}](s)": sec(v) for k, v in self.worker_ticks.items()},
            "재고 일치 기록 수": self.inventory[0],
            "재고 전체 기록 수": self.inventory[1],
            "재고 정확도(%)": self.inventory[2],
            "예약테이블 사전 회피(충돌 예상)": self.avoided_conflicts,
            "예약 어긋남 → 재계획": self.mismatch_replans,
            "물리적 충돌(같은 칸 동시 점유)": 0,
        })
        d.update(self.extra)
        return d

    def amr_rows(self) -> list[dict]:
        ms = self.makespan
        rows = []
        for a in self.amrs:
            involved = {k: sum(1 for e in self.episodes if e.kind == k and a.name in e.amrs) for k in KINDS}
            rows.append({
                "AMR": a.name, "처리건수": a.jobs, "이동거리(m)": a.distance, "이동시간(s)": sec(a.move),
                "작업시간(s)": sec(a.work), "대기시간(s)": sec(a.wait), "유휴시간(s)": sec(a.idle),
                "태그대기(s)": sec(a.tag_wait), "고장(s)": sec(a.down), "정지 횟수": a.stops,
                "가동률(%)": self.utilization(a), "시간당 처리(건/h)": a.jobs / (ms / 3600),
                "경로충돌 관여": involved["경로충돌"], "정면충돌 관여": involved["정면충돌"],
                "병목 횟수": involved["병목"], "장시간정체 개입": involved["장시간정체"],
            })
        return rows


def _fmt(v):
    return f"{v:.1f}" if isinstance(v, float) else ("" if v is None else v)


def _write(path, header, rows):
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(header)
        for r in rows:
            w.writerow([_fmt(v) for v in r])


def save_order_log(res: SimResult, path: str):
    header = ["주문ID", "저장구역", "발생시각(s)", "작업시작(배정)시각(s)", "입고장 도착(s)", "RFID 통과(s)",
              "저장구역 처리완료(s)", "출고완료(s)", "총처리시간(s)", "주문 대응시간(s)", "배정 AMR",
              "배정 당시 AMR-입고장 거리(m)", "실제 배차비용(s)", "최소 가능비용(s)", "최소비용 AMR",
              "배차 비효율", "추가 이동거리(m)", "추가 예상시간(s)", "수동 배차 확인 개입",
              "경로충돌", "정면충돌", "병목 횟수", "병목 대기(s)", "장시간정체 개입", "장시간정체 개입시간(s)",
              "이동거리(m)", "이동시간(s)", "작업시간(s)", "대기시간(s)", "입고 토트", "적치 슬롯", "출고 토트", "피킹 슬롯"]
    rows = []
    for o in res.orders:
        p, pt = o.problems, o.problem_ticks
        rows.append([o.order_id, o.rack, sec(o.release), sec(o.assign), sec(o.t_in), sec(o.t_rfid), sec(o.t_rack),
                     sec(o.complete), sec(o.lead), sec(o.response), o.amr, o.assign_dist, sec(o.cost),
                     sec(o.min_cost), o.best_amr, int(o.inefficient), o.extra_dist, sec(o.extra_time),
                     int(o.manual_check), p.get("경로충돌", 0), p.get("정면충돌", 0), p.get("병목", 0),
                     sec(pt.get("병목", 0)), p.get("장시간정체", 0), sec(pt.get("장시간정체", 0)),
                     o.distance, sec(o.move), sec(o.work), sec(o.wait), o.tote_in, o.slot_in, o.tote_out, o.slot_out])
    _write(path, header, rows)


def save_amr_summary(res: SimResult, path: str):
    rows = res.amr_rows()
    _write(path, list(rows[0].keys()), [list(r.values()) for r in rows])


def save_system_summary(results: list[SimResult], path: str):
    rows = [r.system_summary() for r in results]
    keys = list(rows[0].keys())
    for r in rows[1:]:
        keys += [k for k in r if k not in keys]
    _write(path, keys, [[r.get(k) for k in keys] for r in rows])


def save_events(res: SimResult, path: str):
    """문제 에피소드 원본 로그 (카운트 검증용): 종류·AMR·위치·시작·종료·지속시간·관련 주문."""
    rows = [[e.kind, "+".join(e.amrs), e.where, sec(e.start), sec(e.last + 1), sec(e.duration), " ".join(sorted(e.orders))]
            for e in sorted(res.episodes, key=lambda e: e.start)]
    _write(path, ["종류", "AMR", "위치/구역", "시작(s)", "종료(s)", "지속(s)", "관련 주문"], rows)
