"""작업배정 · 경로계산 전략 (교체 가능한 부분).

CASE2 = SimpleDispatcher (먼저 비는 AMR, AMR1→2→3) + IndependentPlanner (각자 A*)
CASE3 = HungarianDispatcher + CooperativePlanner 로 이 두 객체만 갈아끼우면 된다 (미구현).
"""
from __future__ import annotations

from typing import Protocol, Sequence

from .layout import Grid
from .pathfinding import astar, static_path

Cell = tuple[int, int]


class Dispatcher(Protocol):
    def assign(self, now: float, queue: Sequence, available: Sequence) -> list[tuple[object, object]]:
        """대기 작업 queue(발생순) 와 비어있는 자원 available 로 (job, 자원) 쌍 목록을 반환."""


class PathPlanner(Protocol):
    def plan(self, grid: Grid, start: Cell, goal: Cell, agent, now: float,
             avoid: set[Cell] | None = None) -> list[Cell] | None:
        """start→goal 셀 목록 (start 제외). avoid: 이번 계획에서 피할 셀."""


class SimpleDispatcher:
    """FIFO 작업을, 사용 가능한 자원 중 번호가 가장 작은 것부터 배정."""

    def assign(self, now, queue, available):
        free = sorted(available, key=lambda a: a.idx)
        return list(zip(list(queue)[:len(free)], free))


class IndependentPlanner:
    """다른 AMR 의 미래 위치를 고려하지 않는 독립 최단경로 (A*)."""

    def plan(self, grid, start, goal, agent, now, avoid=None):
        if avoid:
            return astar(grid, start, goal, avoid)
        return list(static_path(start, goal))


# ---------------------------------------------------------------- CASE3 자리
class HungarianDispatcher:
    """(CASE3 예정) 대기 작업 x 가용 AMR 비용행렬에 헝가리안 알고리즘 적용."""

    def assign(self, now, queue, available):
        raise NotImplementedError("CASE3 에서 구현 예정")


class CooperativePlanner:
    """(CASE3 예정) Cooperative A* + Reservation Table (시공간 예약)."""

    def plan(self, grid, start, goal, agent, now, avoid=None):
        raise NotImplementedError("CASE3 에서 구현 예정")
