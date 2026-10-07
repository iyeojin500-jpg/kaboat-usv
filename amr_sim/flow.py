"""작업(Task) 처리 흐름과 예상시간 — 엔진·배정기·배차비용 판정이 함께 사용 (자유주행 기준).

  입고 작업: 입고장(IN) 적재 → RFID 구역 인식 → 저장구역 적치 + WMS
  출고 작업: 저장구역 피킹 + WMS → 출고장(OUT) 하역 + WMS
"""
from __future__ import annotations

from . import params as P
from .layout import INBOUND_STATION, OUTBOUND_STATION, RFID_GATE
from .motion import path_ticks

T = P.ticks


def rfid_ticks(order) -> int:
    return T(P.RFID_READ) + (T(P.WORKER_INTERVENTION) if order.rfid == "FAIL" else 0)


def place_ticks(order) -> int:
    return T(order.t_place) + T(P.RFID_WMS)


def pick_ticks(order) -> int:
    return T(order.t_pick) + T(P.RFID_WMS)


def out_ticks(order) -> int:
    return T(order.t_unload) + T(P.RFID_WMS)


def service_ticks(task, rack_cell=None) -> int:
    """작업 시작 위치에 도착한 뒤 작업 완료까지 예상 tick (태그 대기 제외)."""
    o = task.order
    rack = rack_cell or o.rack_cell
    if task.kind == "IN":
        return (T(o.t_load) + path_ticks(INBOUND_STATION, RFID_GATE, True) + rfid_ticks(o)
                + path_ticks(RFID_GATE, rack, True) + place_ticks(o))
    return pick_ticks(o) + path_ticks(rack, OUTBOUND_STATION, True) + out_ticks(o)


def remaining_ticks(a) -> int:
    """AMR a 가 현재 작업을 끝낼 때까지 남은 예상 tick."""
    task, o = a.job, a.job.order
    rem = sum(t[1] for t in a.tasks)
    rack = a.rack_cell or o.rack_cell
    st = a.stage
    if task.kind == "IN":
        to_rack = path_ticks(RFID_GATE, rack, True) + place_ticks(o)
        if st == "TO_IN":
            rem += path_ticks(a.pos, INBOUND_STATION, False) + service_ticks(task, rack)
        elif st == "AT_IN":
            rem += path_ticks(INBOUND_STATION, RFID_GATE, True) + rfid_ticks(o) + to_rack
            if not a.tasks:
                rem += T(o.t_load)
        elif st == "TO_RFID":
            rem += path_ticks(a.pos, RFID_GATE, True) + rfid_ticks(o) + to_rack
        elif st == "AT_RFID":
            rem += to_rack
        elif st == "TO_RACK":
            rem += path_ticks(a.pos, rack, True) + place_ticks(o)
    else:
        if st == "TO_PICK":
            rem += path_ticks(a.pos, rack, False) + service_ticks(task, rack)
        elif st == "AT_PICK":
            rem += path_ticks(rack, OUTBOUND_STATION, True) + out_ticks(o)
        elif st == "TO_OUT":
            rem += path_ticks(a.pos, OUTBOUND_STATION, True) + out_ticks(o)
    return rem


def task_end_cell(a):
    """AMR a 의 현재 작업이 끝나는 위치."""
    task = a.job
    if task.kind == "IN":
        return a.rack_cell or task.order.rack_cell
    return OUTBOUND_STATION
