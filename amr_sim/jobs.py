"""주문 1,000건 생성 + WMS/실물 재고 모델.

주문 1건 = 입고장에서 토트 적재 → RFID 구역 인식 → 저장구역(A~J) 에서 입고 토트 적치 + 출고 토트 피킹
          → 중앙 교차로 → 좁은 통로 → 출고장 하역 (O0001 ~ O1000)

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

    def choose_slots(self, rack: str) -> tuple[str | None, str | None]:
        """(적치할 빈 슬롯, 피킹할 슬롯). 재고 가정상 피킹 슬롯은 항상 있음."""
        free = [s for s, v in self.actual[rack].items() if v is None and s not in self.reserved]
        place = free[0] if free else None
        pick = next((s for s in self.fifo[rack] if s not in self.reserved), None)
        self.reserved.update(x for x in (place, pick) if x)
        return place, pick

    def process(self, order: Order, place: str | None, pick: str | None) -> tuple[str, str | None]:
        """랙에서 적치+피킹 실행. (입고 토트 ID, 출고 토트 ID) 반환."""
        rack = order.rack
        self.reserved.difference_update((place, pick))
        tote_in = self.new_tote()
        tote_out = None
        if pick is not None:
            tote_out = self.actual[rack][pick]
            self.actual[rack][pick] = None
            self.wms[rack][pick] = None
            self.fifo[rack].remove(pick)
        if place is not None:
            self.actual[rack][place] = tote_in
            self.wms[rack][place] = tote_in if order.rfid != RFID_MISREAD else f"{tote_in}?"
            self.fifo[rack].append(place)
            self.records_total += 1
            self.records_match += order.rfid != RFID_MISREAD
        return tote_in, tote_out

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
