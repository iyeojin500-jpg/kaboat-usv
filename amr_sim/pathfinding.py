"""최단경로 탐색 (4방향 A*, 1칸 = 1m)."""
from __future__ import annotations

import heapq
from functools import lru_cache

from .layout import Grid, build_grid

Cell = tuple[int, int]

_GRID = build_grid()


def manhattan(a: Cell, b: Cell) -> int:
    return abs(a[0] - b[0]) + abs(a[1] - b[1])


TURN_COST = 1        # 길이 1칸 = 1000, 회전 1회 = 1 → 최단거리 중 회전이 가장 적은 경로


def astar(grid: Grid, start: Cell, goal: Cell, obstacles: frozenset[Cell] | set[Cell] = frozenset()) -> list[Cell] | None:
    """start→goal 최단경로 (start 제외, goal 포함). 도달 불가면 None.

    길이가 같은 경로가 여럿이면 회전 수가 가장 적은 경로를 고른다 (지그재그 방지).
    obstacles: 정적 장애물(랙) 외에 추가로 피할 셀 (예: 다른 AMR 가 서 있는 셀).
    """
    if start == goal:
        return []
    if not grid.passable(goal):
        return None
    counter = 0
    s0 = (start, None)
    open_heap = [(manhattan(start, goal) * 1000, 0, counter, s0)]
    came: dict = {}
    g_cost = {s0: 0}
    while open_heap:
        _, g, _, state = heapq.heappop(open_heap)
        cur, head = state
        if cur == goal:
            path = []
            while state != s0:
                path.append(state[0])
                state = came[state]
            return path[::-1]
        if g > g_cost[state]:
            continue
        for n in grid.neighbors(cur):
            if n in obstacles and n != goal:
                continue
            d = (n[0] - cur[0], n[1] - cur[1])
            ng = g + 1000 + (TURN_COST if head is not None and d != head else 0)
            ns = (n, d)
            if ng < g_cost.get(ns, 1 << 60):
                g_cost[ns] = ng
                came[ns] = state
                counter += 1
                heapq.heappush(open_heap, (ng + manhattan(n, goal) * 1000, ng, counter, ns))
    return None


@lru_cache(maxsize=None)
def static_path(start: Cell, goal: Cell) -> tuple[Cell, ...]:
    """랙만 장애물로 본 최단경로 (캐시)."""
    p = astar(_GRID, start, goal)
    if p is None:
        raise ValueError(f"경로 없음: {start} -> {goal}")
    return tuple(p)


def distance(start: Cell, goal: Cell) -> int:
    return len(static_path(start, goal))


def distance_matrix(points: list[Cell]) -> list[list[int]]:
    """[레이어 1] 지점 간 A* 최단거리(m) 행렬."""
    return [[distance(a, b) for b in points] for a in points]
