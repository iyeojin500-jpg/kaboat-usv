"""주문 1건의 처리 흐름과 예상시간 (엔진·배정기·배차비용 판정이 함께 사용).

  입고장(IN) 적재 → RFID 구역 인식 → 저장구역 적치+피킹 → 출고장(OUT) 하역
"""
from __future__ import annotations

from . import params as P
from .layout import INBOUND_STATION, OUTBOUND_STATION, RFID_GATE
from .motion import path_ticks

T = P.ticks


def rfid_ticks(order) -> int:
    return T(P.RFID_READ) + (T(P.WORKER_INTERVENTION) if order.rfid == "FAIL" else 0)


def rack_ticks(order) -> int:
    return T(order.t_place) + T(order.t_pick) + T(P.RFID_WMS)


def out_ticks(order) -> int:
    return T(order.t_unload) + T(P.RFID_WMS)


def service_ticks(order) -> int:
    """입고장 도착 후 주문 완료까지 예상 tick (자유주행, 태그 대기 제외)."""
    return (T(order.t_load) + path_ticks(INBOUND_STATION, RFID_GATE, True) + rfid_ticks(order)
            + path_ticks(RFID_GATE, order.rack_cell, True) + rack_ticks(order)
            + path_ticks(order.rack_cell, OUTBOUND_STATION, True) + out_ticks(order))


def remaining_ticks(a) -> int:
    """AMR a 가 현재 주문을 끝낼 때까지 남은 예상 tick (자유주행 기준)."""
    o = a.job
    rem = sum(t[1] for t in a.tasks)
    rack = a.rack_cell or o.rack_cell
    after_rfid = path_ticks(RFID_GATE, rack, True) + rack_ticks(o) + path_ticks(rack, OUTBOUND_STATION, True) + out_ticks(o)
    after_rack = path_ticks(rack, OUTBOUND_STATION, True) + out_ticks(o)
    stage = a.stage
    if stage == "TO_IN":
        rem += path_ticks(a.pos, INBOUND_STATION, False) + service_ticks(o)
    elif stage == "AT_IN":
        rem += path_ticks(INBOUND_STATION, RFID_GATE, True) + rfid_ticks(o) + after_rfid
        if not a.tasks:
            rem += T(o.t_load)
    elif stage == "TO_RFID":
        rem += path_ticks(a.pos, RFID_GATE, True) + rfid_ticks(o) + after_rfid
    elif stage == "AT_RFID":
        rem += after_rfid
    elif stage == "TO_RACK":
        rem += path_ticks(a.pos, rack, True) + rack_ticks(o) + after_rack
    elif stage == "AT_RACK":
        rem += after_rack
    elif stage == "TO_OUT":
        rem += path_ticks(a.pos, OUTBOUND_STATION, True) + out_ticks(o)
    return rem
