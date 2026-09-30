"""WMS 재고 모델 + 작업(JOB) 1,000건 생성 (구현 순서 3단계).

- 입고 500 + 출고 500 을 랜덤으로 섞음 (seed=42)
- 발생 간격: 1~200 일반(15~25s), 201~400 Peak1(3~7s), 401~600 일반,
  601~800 Peak2(3~7s), 801~1000 일반
- 공간적 병목: Peak 구간 작업의 약 60% 를 C·D·H·I 랙에 집중
- 재고 가정: 출고 대상 토트는 항상 존재 (생성 시 WMS 로 실제 보유 토트를 고름)
"""
from __future__ import annotations

import csv
import random
from collections import deque
from dataclasses import dataclass

from . import params as P
from .layout import INBOUND_STATION, OUTBOUND_STATION, RACK_CAPACITY, RACKS, SLOT_COLS, SLOT_LEVELS

N_INBOUND = 500
N_OUTBOUND = 500
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


@dataclass(frozen=True)
class Job:
    job_id: int
    kind: str                 # "INBOUND" | "OUTBOUND"
    tote_id: str
    item: str
    rack: str
    slot: str                 # 랙 슬롯 ID (입고=적치할 빈 슬롯, 출고=토트가 있는 슬롯)
    origin: str
    destination: str
    release: float            # 발생시각 (초)
    peak: bool
    # 두 CASE 공통으로 쓰도록 미리 뽑아 둔 작업시간(초)·RFID 결과
    t_first: float = 10.0     # 입고=입고장 적재, 출고=랙 피킹
    t_second: float = 10.0    # 입고=랙 적치, 출고=출고장 하역
    t_tag: float = 0.0        # 입고 RFID 태그 부착 (사람)
    rfid_fail: bool = False   # RFID 자동 인식 실패 → 작업자 개입

    @property
    def slot_cell(self) -> tuple[int, int]:
        col = int(self.slot.split("-")[1])
        return RACKS[self.rack].slot_access(col)

    @property
    def pickup_cell(self) -> tuple[int, int]:
        return INBOUND_STATION if self.kind == "INBOUND" else self.slot_cell

    @property
    def drop_cell(self) -> tuple[int, int]:
        return self.slot_cell if self.kind == "INBOUND" else OUTBOUND_STATION


class WMS:
    """토트 위치·상태 관리. 슬롯 → (토트ID, 품목)."""

    def __init__(self, rng: random.Random):
        self.rng = rng
        self.slots: dict[str, dict[str, tuple[str, str] | None]] = {}
        self.free: dict[str, deque[str]] = {}
        self._next_tote = 1
        for name, rack in RACKS.items():
            all_slots = rack.slots()
            filled = set(rng.sample(all_slots, INITIAL_PER_RACK))
            self.slots[name] = {s: (self.new_tote(), rng.choice(ITEMS[name])) if s in filled else None
                                for s in all_slots}
            empty = [s for s in all_slots if s not in filled]
            rng.shuffle(empty)
            self.free[name] = deque(empty)

    def new_tote(self) -> str:
        tid = f"T-{self._next_tote:04d}"
        self._next_tote += 1
        return tid

    def count(self, rack: str) -> int:
        return RACK_CAPACITY - len(self.free[rack])

    def total(self) -> int:
        return sum(self.count(r) for r in RACKS)

    def store(self, rack: str, item: str) -> tuple[str, str]:
        # 가장 오래 비어 있던 슬롯부터 사용 → 방금 출고된 슬롯 재사용을 늦춤
        slot = self.free[rack].popleft()
        tote = self.new_tote()
        self.slots[rack][slot] = (tote, item)
        return tote, slot

    def retrieve(self, rack: str) -> tuple[str, str, str]:
        occupied = [s for s, v in self.slots[rack].items() if v is not None]
        slot = self.rng.choice(occupied)
        tote, item = self.slots[rack][slot]
        self.slots[rack][slot] = None
        self.free[rack].append(slot)
        return tote, item, slot

    def record(self, rack: str, slot: str) -> str:
        v = self.slots[rack][slot]
        return f"{v[0]} / {v[1]} / {rack}랙 / {slot} / 보관중" if v else f"{slot} / 비어있음"


def is_peak(job_no: int) -> bool:
    return any(a <= job_no <= b for a, b in PEAK_RANGES)


def _choose_rack(rng: random.Random, candidates: list[str], peak: bool, spatial: bool) -> str:
    if peak and spatial:
        hot = [r for r in candidates if r in HOT_RACKS]
        cold = [r for r in candidates if r not in HOT_RACKS]
        if hot and (not cold or rng.random() < HOT_RATIO):
            return rng.choice(hot)
        return rng.choice(cold)
    return rng.choice(candidates)


def generate_jobs(seed: int = 42, spatial_bottleneck: bool = True) -> list[Job]:
    rng = random.Random(seed)
    kinds = ["INBOUND"] * N_INBOUND + ["OUTBOUND"] * N_OUTBOUND
    rng.shuffle(kinds)
    wms = WMS(rng)
    op_rng = random.Random(seed + 1)   # 작업시간·RFID 결과 전용 (주문 목록 자체에는 영향 없음)
    jobs: list[Job] = []
    t = 0.0
    for i, kind in enumerate(kinds, start=1):
        peak = is_peak(i)
        if i > 1:
            t += rng.uniform(*(PEAK_GAP if peak else NORMAL_GAP))
        if kind == "INBOUND":
            cands = [r for r in RACKS if wms.free[r]]
            rack = _choose_rack(rng, cands, peak, spatial_bottleneck)
            item = rng.choice(ITEMS[rack])
            tote, slot = wms.store(rack, item)
            origin, dest = "입고STATION", slot
        else:
            cands = [r for r in RACKS if wms.count(r) > 0]
            rack = _choose_rack(rng, cands, peak, spatial_bottleneck)
            tote, item, slot = wms.retrieve(rack)
            origin, dest = slot, "출고STATION"
        if kind == "INBOUND":
            t1, t2, tag = P.jitter(op_rng, P.AMR_LOAD), P.jitter(op_rng, P.AMR_PLACE), P.jitter(op_rng, P.RFID_TAG)
        else:
            t1, t2, tag = P.jitter(op_rng, P.AMR_PICK), P.jitter(op_rng, P.AMR_UNLOAD), 0.0
        fail = op_rng.random() < P.RFID_FAIL_RATE
        jobs.append(Job(i, kind, tote, item, rack, slot, origin, dest, round(t, 2), peak,
                        round(t1, 1), round(t2, 1), round(tag, 1), fail))
    return jobs


def save_jobs_csv(jobs: list[Job], path: str) -> None:
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["JOB_ID", "종류", "토트ID", "품목", "분류(랙)", "슬롯", "출발지", "목적지", "발생시각", "구간",
                    "작업시간1", "작업시간2", "태그부착", "RFID실패"])
        for j in jobs:
            w.writerow([j.job_id, j.kind, j.tote_id, j.item, j.rack, j.slot, j.origin, j.destination,
                        f"{j.release:.2f}", "PEAK" if j.peak else "일반",
                        j.t_first, j.t_second, j.t_tag, int(j.rfid_fail)])
