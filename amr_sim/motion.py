"""AMR 이동 시간 모델 — 엔진·예약테이블·OR-Tools 가 모두 같은 함수를 쓴다."""
from __future__ import annotations

from functools import lru_cache

from . import params as P
from .layout import INTERSECTIONS
from .pathfinding import static_path

Cell = tuple[int, int]


def traverse_ticks(cell: Cell, nxt: Cell, heading, loaded: bool) -> int:
    """cell → nxt 한 칸(1m) 이동 tick 수. 회전 칸 0.6m/s, 교차로 진입 0.8m/s."""
    v = P.AMR_SPEED_LOADED if loaded else P.AMR_SPEED_EMPTY
    d = (nxt[0] - cell[0], nxt[1] - cell[1])
    if heading is not None and d != heading:
        v = min(v, P.AMR_SPEED_TURN)
    if nxt in INTERSECTIONS:
        v = min(v, P.AMR_SPEED_CROSS)
    return P.ticks(1.0 / v)


MIN_TICKS_PER_CELL = P.ticks(1.0 / P.AMR_SPEED_EMPTY)   # A* 휴리스틱 (하한)


@lru_cache(maxsize=None)
def path_ticks(start: Cell, goal: Cell, loaded: bool) -> int:
    """다른 AMR 가 없을 때(자유 주행) A* 최단경로 이동 tick 수."""
    total, cur, head = 0, start, None
    for n in static_path(start, goal):
        total += traverse_ticks(cur, n, head, loaded)
        head = (n[0] - cur[0], n[1] - cur[1])
        cur = n
    return total
