"""작업배정 · 경로계산 전략 — 엔진에 끼우는 부품.

CASE1 = SimpleDispatcher (먼저 비는 AMR 에 먼저 온 주문) + IndependentPlanner (각자 A*, 충돌 시 대기)
CASE2 = ORToolsDispatcher (dispatch_ortools.py)     + CooperativePlanner (reservation.py)
"""
from __future__ import annotations

from .layout import Grid
from .pathfinding import astar, static_path


class SimpleDispatcher:
    """FIFO 주문을, 비어있는 AMR 중 번호가 가장 작은 것부터 배정 (AMR1→2→3)."""
    name = "단순 순차"

    def assign(self, sim, now, queue, available):
        free = sorted(available, key=lambda a: a.idx)
        return list(zip(list(queue)[:len(free)], free))


class IndependentPlanner:
    """다른 AMR 의 미래 위치를 고려하지 않는 독립 최단경로 (A*)."""
    name = "독립 A*"
    cooperative = False

    def __init__(self, grid: Grid):
        self.grid = grid

    def plan(self, start, goal, avoid=None):
        if avoid:
            return astar(self.grid, start, goal, avoid)
        return list(static_path(start, goal))
