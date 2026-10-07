"""주문 1,000건 생성 + 작업(Task) 분리 + WMS/실물 재고 모델.

주문 1건 (O0001 ~ O1000) = 작업 2개
  입고 작업 (IN)  : 입고장 적재 → RFID 구역 인식 → 저장구역(A~J) 빈 슬롯에 적치
  출고 작업 (OUT) : 저장구역에서 실제 화물(FIFO) 피킹 → 중앙 교차로 → 좁은 통로 → 출고장 하역
  선행조건: 같은 주문의 입고 작업이 끝나야 출고 작업을 시작할 수 있다. 주문 완료 = 출고 작업 완료.

- 발생 간격: 1~200 일반(15~25s), 201~400 피크(3~7s), 401~600 일반, 601~800 피크, 801~1000 일반
- 저장구역 A~J 랜덤 (피크 구간은 약 60% 를 C·D·H·I 에 집중 — 공간적 병목)
- 작업시간·RFID 결과는 생성 시 주문마다 미리 뽑아 Before/After 에 똑같이 적용 (seed 고정)
- 재고 가정: 출고할 토트는 항상 있음. 어느 슬롯을 쓰는지는 실제 처리 순서에 따라 Inventory 가 정한다.
"""
from __future__ import annotations

import csv
import random
from collections import deque
from dataclasses import dataclass

from . import params as P
from .layout import RACK_CAPACITY, RACKS

N_ORDERS = 1000
INITIAL_PER_RACK = 15
HOT_RACKS = ("C", "D", "H", "I")
HOT_RATIO = 0.6
NORMAL_GAP = (15.0, 25.0)
PEAK_GAP = (3.0, 7.0)
PEAK_RANGES = ((201, 400), (601, 800))

ITEMS = {
    "A": ["볼트 M8", "너트 M8", "와셔 8mm", "나사 M4"],
    "B": ["베어링 6204", "부싱 20mm", "소형 롤러"],
    "C": ["전선 2.5sq", "제어 케이블", "통신 케이블"],
    "D": ["커넥터 8P", "터미널 블록", "소켓 16P"],
    "E": ["근접 센서", "온도 센서", "광 센서"],
    "F": ["푸시버튼", "리미트스위치", "토글스위치"],
    "G": ["릴레이 24V", "PCB 보드", "컨트롤 모듈"],
    "H": ["어댑터 24V", "전원공급장치 150W", "퓨즈 5A"],
    "I": ["원터치 피팅", "솔레노이드 밸브", "튜브 연결구"],
    "J": ["케이블타이", "절연테이프", "클립"],
}

RFID_OK, RFID_FAIL, RFID_MISREAD = "OK", "FAIL", "MISREAD"


@dataclass(frozen=True)
class Order:
    job_id: int
    rack: str
    item: str
    release: float            # 발생시각 (초)
    peak: bool
    t_tag: float              # 입고장 RFID 태그 부착 (사람)
    t_load: float             # 입고장 적재
    t_place: float            # 랙 적치
    t_pick: float             # 랙 피킹
    t_unload: float           # 출고장 하역
    rfid: str                 # OK / FAIL(감지 → 작업자 개입) / MISREAD(미감지 → WMS 불일치)

    @property
    def order_id(self) -> str:
        return f"O{self.job_id:04d}"

    @property
    def rack_cell(self):
        """배정·예상비용 계산용 대표 접근점 (실제 칸은 처리 시 슬롯에 따라 정해짐)."""
        return RACKS[self.rack].representative


def is_peak(no: int) -> bool:
    return any(a <= no <= b for a, b in PEAK_RANGES)


def generate_orders(seed: int = P.SEED, spatial_bottleneck: bool = True, gap_scale: float = 1.0) -> list[Order]:
    """gap_scale: 발생 간격 배율 (민감도 확인용, 기본 1.0 = 설계 타임라인 그대로)."""
    rng = random.Random(seed)
    op = random.Random(seed + 1)      # 작업시간·RFID 결과 전용 스트림
    racks = list(RACKS)
    hot = [r for r in racks if r in HOT_RACKS]
    cold = [r for r in racks if r not in HOT_RACKS]
    orders, t = [], 0.0
    for i in range(1, N_ORDERS + 1):
        peak = is_peak(i)
        if i > 1:
            t += rng.uniform(*(PEAK_GAP if peak else NORMAL_GAP)) * gap_scale
        if peak and spatial_bottleneck:
            rack = rng.choice(hot if rng.random() < HOT_RATIO else cold)
        else:
            rack = rng.choice(racks)
        u = op.random()
        rfid = RFID_FAIL if u < P.RFID_FAIL_RATE else RFID_MISREAD if u < P.RFID_FAIL_RATE + P.RFID_MISREAD_RATE else RFID_OK
        orders.append(Order(
            i, rack, rng.choice(ITEMS[rack]), round(t, 2), peak,
            round(P.jitter(op, P.RFID_TAG), 1), round(P.jitter(op, P.AMR_LOAD), 1),
            round(P.jitter(op, P.AMR_PLACE), 1), round(P.jitter(op, P.AMR_PICK), 1),
            round(P.jitter(op, P.AMR_UNLOAD), 1), rfid))
    return orders


generate_jobs = generate_orders   # 이전 이름 호환


class Task:
    """AMR 1대가 한 번에 수행하는 작업 단위. kind = "IN"(입고) / "OUT"(출고)."""

    __slots__ = ("order", "kind", "ready", "amr")

    def __init__(self, order: Order, kind: str, ready: int):
        self.order = order
        self.kind = kind
        self.ready = ready          # 작업 가능해진 tick (입고=주문 발생, 출고=입고 완료)
        self.amr = ""

    @property
    def order_id(self) -> str:
        return self.order.order_id

    @property
    def job_id(self) -> int:
        return self.order.job_id

    @property
    def rack(self) -> str:
        return self.order.rack

    @property
    def release(self) -> float:
        return self.order.release

    @property
    def label(self) -> str:
        return f"{self.order_id}-{'입고' if self.kind == 'IN' else '출고'}"

    @property
    def priority(self):
        """우선순위: 먼저 발생한 주문 먼저, 같은 주문이면 입고 → 출고."""
        return (self.order.release, self.order.job_id, self.kind == "OUT")

    @property
    def start_cell(self):
        from .layout import INBOUND_STATION
        return INBOUND_STATION if self.kind == "IN" else self.order.rack_cell

    @property
    def end_cell(self):
        from .layout import OUTBOUND_STATION
        return self.order.rack_cell if self.kind == "IN" else OUTBOUND_STATION


class Inventory:
    """실물 재고와 WMS 기록을 따로 들고 있다가 재고 정확도를 계산한다.

    랙 처리 시: 입고 토트를 빈 슬롯에 적치하고, 가장 오래 보관된 토트(FIFO)를 피킹한다.
    RFID 오인식(MISREAD) 주문은 WMS 에 다른 토트 ID 로 기록되어 실물과 불일치가 남는다.
    """

    def __init__(self, seed: int = P.SEED):
        rng = random.Random(seed + 2)
        self.actual: dict[str, dict[str, str | None]] = {}
        self.wms: dict[str, dict[str, str | None]] = {}
        self.fifo: dict[str, deque[str]] = {}
        self.reserved: set[str] = set()        # 다른 AMR 가 처리하러 가는 중인 슬롯
        self._next = 1
        for name, rack in RACKS.items():
            slots = rack.slots()
            filled = rng.sample(slots, INITIAL_PER_RACK)
            self.actual[name] = {s: None for s in slots}
            self.fifo[name] = deque()
            for s in filled:
                tid = self.new_tote()
                self.actual[name][s] = tid
                self.fifo[name].append(s)
            self.wms[name] = dict(self.actual[name])
        self.records_total = sum(len(f) for f in self.fifo.values())   # 누적 재고 기록 수 (초기 재고 포함)
        self.records_match = self.records_total

    def new_tote(self) -> str:
        tid = f"T-{self._next:04d}"
        self._next += 1
        return tid

    def has_stock(self, rack: str) -> bool:
        return bool(self.fifo[rack])

    def can_place(self, rack: str) -> bool:
        """입고 가능: 예약되지 않은 실제 빈 슬롯이 있는가."""
        return any(v is None and s not in self.reserved for s, v in self.actual[rack].items())

    def can_pick(self, rack: str) -> bool:
        """출고 가능: 예약되지 않은 실제 화물이 있는가."""
        return any(s not in self.reserved for s in self.fifo[rack])

    def choose_place(self, rack: str) -> str | None:
        """입고 위치 선정 규칙: 랙 안의 빈 슬롯 중 슬롯 번호 순 첫 번째 (예약)."""
        free = [s for s, v in self.actual[rack].items() if v is None and s not in self.reserved]
        if free:
            self.reserved.add(free[0])
            return free[0]
        return None

    def choose_pick(self, rack: str) -> str | None:
        """출고 위치 선정 규칙: 가장 오래 보관된 화물(FIFO) (예약)."""
        pick = next((s for s in self.fifo[rack] if s not in self.reserved), None)
        if pick:
            self.reserved.add(pick)
        return pick

    def store(self, order: Order, place: str | None) -> str:
        """입고 작업: 입고 토트를 빈 슬롯에 적치. RFID 오인식이면 WMS 에 다른 ID 로 기록."""
        tote_in = self.new_tote()
        if place is not None:
            self.reserved.discard(place)
            rack = order.rack
            self.actual[rack][place] = tote_in
            self.wms[rack][place] = tote_in if order.rfid != RFID_MISREAD else f"{tote_in}?"
            self.fifo[rack].append(place)
            self.records_total += 1
            self.records_match += order.rfid != RFID_MISREAD
        return tote_in

    def retrieve(self, rack: str, pick: str | None) -> str | None:
        """출고 작업: 화물 피킹."""
        if pick is None:
            return None
        self.reserved.discard(pick)
        tote_out = self.actual[rack][pick]
        self.actual[rack][pick] = None
        self.wms[rack][pick] = None
        self.fifo[rack].remove(pick)
        return tote_out

    def accuracy(self) -> tuple[int, int, float]:
        """누적 재고 정확도 (일치 기록 수, 전체 기록 수, %) — 초기 재고 + 실행 중 적치된 모든 토트 기록 중
        WMS 기록이 실물과 일치한 비율. (오인식 토트가 나중에 출고돼도 기록 오류는 남는다)"""
        t, m = self.records_total, self.records_match
        return m, t, 100.0 * m / t if t else 100.0

    def snapshot_accuracy(self) -> tuple[int, int, float]:
        """종료 시점 재고 정확도 — 지금 보관 중인 토트 기준."""
        total = match = 0
        for rack in self.actual:
            for s, v in self.actual[rack].items():
                if v is not None:
                    total += 1
                    match += self.wms[rack][s] == v
        return match, total, 100.0 * match / total if total else 100.0

    def capacity_ok(self) -> bool:
        return all(sum(v is not None for v in self.actual[r].values()) <= RACK_CAPACITY for r in RACKS)


def save_orders_csv(orders: list[Order], path: str) -> None:
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["주문ID", "저장구역", "품목", "발생시각", "구간", "태그부착", "적재", "적치", "피킹", "하역", "RFID"])
        for o in orders:
            w.writerow([o.order_id, o.rack, o.item, f"{o.release:.2f}", "PEAK" if o.peak else "일반",
                        o.t_tag, o.t_load, o.t_place, o.t_pick, o.t_unload, o.rfid])


save_jobs_csv = save_orders_csv
