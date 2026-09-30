"""Cooperative A* + 예약테이블 (CASE2 경로 실행 층).

- 예약테이블: 칸마다 [시작 tick, 끝 tick] 점유 구간 목록. 이동 중인 AMR 는 출발 칸과 도착 칸을 모두 점유.
- AMR 는 한 대씩 순서대로(계획 요청 순) 시공간 A* 로 경로를 찾고, 찾은 (칸, 시각) 을 예약한다.
  이후 계획하는 AMR 는 이미 예약된 구간을 피한다 → 같은 칸·같은 시각 동시 진입이 원천 차단된다.
- 목적지에 도착한 AMR 는 다음 계획을 세울 때까지 그 칸을 무기한 점유(예약)한다.
- 대기 후 출발하면 재출발 지연 1초를 경로 시간에 포함한다 (엔진과 동일 규칙).
"""
from __future__ import annotations

import heapq
from collections import defaultdict
from functools import lru_cache

from . import params as P
from .layout import Grid
from .motion import traverse_ticks
from .pathfinding import static_path

INF = 1 << 60
WAIT_STEP = 5            # 대기 행동 단위 (tick = 0.5초)
MAX_EXPANSIONS = 15000
HORIZON = 3000           # 계획 최대 길이 (tick = 300초)


DIRS = ((1, 0), (-1, 0), (0, 1), (0, -1))


@lru_cache(maxsize=None)
def _time_to_go(grid_id: int, goal, loaded: bool) -> dict:
    """자유주행 기준 (칸, 진행방향) → goal 최소 tick (회전·교차로 감속 포함, 역방향 다익스트라).
    다른 AMR 가 없을 때의 정확한 값이라 시공간 A* 의 휴리스틱으로 쓰면 탐색이 크게 준다."""
    grid = _GRIDS[grid_id]
    H = {}
    heap = []
    H[(goal, None)] = 0
    for d in DIRS:
        H[(goal, d)] = 0
        heapq.heappush(heap, (0, goal, d))
    while heap:
        v, c, d = heapq.heappop(heap)
        if v > H.get((c, d), INF) or d is None:
            continue
        p = (c[0] - d[0], c[1] - d[1])
        if not grid.passable(p):
            continue
        for hp in DIRS + (None,):
            nv = v + traverse_ticks(p, c, hp, loaded)
            if nv < H.get((p, hp), INF):
                H[(p, hp)] = nv
                if hp is not None:
                    heapq.heappush(heap, (nv, p, hp))
    return H


_GRIDS: dict[int, Grid] = {}


class ReservationTable:
    def __init__(self):
        self.cells: dict[tuple, list[list]] = defaultdict(list)   # cell -> [[s, e, agent_idx], ...]
        self.owned: dict[int, list] = defaultdict(list)            # agent -> [(cell, entry), ...]

    def reserve(self, cell, s: int, e: int, agent: int):
        entry = [s, e, agent]
        self.cells[cell].append(entry)
        self.owned[agent].append((cell, entry))

    def release_agent(self, agent: int):
        for cell, entry in self.owned.pop(agent, []):
            try:
                self.cells[cell].remove(entry)
            except ValueError:
                pass

    def is_free(self, cell, s: int, e: int, agent: int) -> bool:
        for s2, e2, a2 in self.cells.get(cell, ()):
            if a2 != agent and s <= e2 and s2 <= e:
                return False
        return True

    def held_forever_by_other(self, cell, agent: int) -> bool:
        return any(e == INF and a != agent for _, e, a in self.cells.get(cell, ()))

    def forever_holders(self, cell, agent: int) -> list:
        return [x for x in self.cells.get(cell, ()) if x[1] == INF and x[2] != agent]

    def purge(self, now: int):
        for cell, lst in self.cells.items():
            lst[:] = [x for x in lst if x[1] >= now]
        for agent, lst in self.owned.items():
            lst[:] = [(c, x) for c, x in lst if x[1] >= now]


class CooperativePlanner:
    """시공간 A* 로 예약테이블과 충돌 없는 경로를 찾는다."""

    name = "Cooperative A* + 예약테이블"
    cooperative = True

    def __init__(self, grid: Grid):
        self.grid = grid
        _GRIDS[id(grid)] = grid
        self.table = ReservationTable()
        self.predicted_conflicts = 0   # 독립 최단경로였다면 충돌했을 계획 수
        self.detours = 0               # 최단경로 대비 더 길게(우회·대기) 계획한 수
        self.asides = 0                # 교착 해소용 비켜서기 횟수

    def park(self, agent: int, cell, now: int):
        self.table.reserve(cell, now, INF, agent)

    def _free_flow_conflict(self, agent, start, goal, now, heading, loaded) -> bool:
        cur, head, t = start, heading, now
        for n in static_path(start, goal):
            d = traverse_ticks(cur, n, head, loaded)
            if not self.table.is_free(cur, t, t + d - 1, agent) or not self.table.is_free(n, t, t + d - 1, agent):
                return True
            head, cur, t = (n[0] - cur[0], n[1] - cur[1]), n, t + d
        return not self.table.is_free(cur, t, INF, agent)

    def plan_timed(self, agent: int, start, goal, now: int, heading, loaded: bool, goal_release=None):
        """[(출발 tick, 다음 칸, 이동 tick), ...] 또는 None(지금은 경로 없음 → 잠시 후 재시도).

        goal_release: 목적지를 다른 AMR 가 작업 중으로 점유하고 있을 때, 그 AMR 가 떠날 것으로
        예상되는 tick. 주어지면 그 시각 이후 도착하도록 계획한다 (가까이 가서 기다림).
        """
        tbl = self.table
        tbl.release_agent(agent)
        holders = tbl.forever_holders(goal, agent)
        if holders and goal_release is None:
            # 목적지를 무기한 점유 중인데 떠날 시각을 모름 → 제자리 대기 후 재시도
            self.park(agent, start, now)
            return None
        saved = [(x, x[1]) for x in holders]
        for x in holders:                    # 탐색 동안만 예상 이탈 시각으로 줄여 본다
            x[1] = goal_release
        try:
            return self._search(agent, start, goal, now, heading, loaded)
        finally:
            for x, e in saved:
                x[1] = e

    def _search(self, agent: int, start, goal, now: int, heading, loaded: bool):
        tbl = self.table
        conflict = self._free_flow_conflict(agent, start, goal, now, heading, loaded)

        def free(cell, s, e, moved):
            # 아직 출발 전이면 지금 서 있는 칸은 내가 실제로 점유 중 → 다른 예약보다 우선
            return (not moved and cell == start) or tbl.is_free(cell, s, e, agent)

        H = _time_to_go(id(self.grid), goal, loaded)

        def h(cell, head):
            return H.get((cell, head), INF)

        s0 = (start, heading, now, False, False)
        heap = [(now + h(start, heading), -now, 0, s0)]
        parent = {s0: None}
        seen = set()
        counter = 0
        found = None
        while heap and counter < MAX_EXPANSIONS:
            _, _, _, st = heapq.heappop(heap)
            if st in seen:
                continue
            seen.add(st)
            cell, head, t, stopped, moved = st
            if cell == goal and tbl.is_free(cell, t, INF, agent):
                found = st
                break
            if t - now > HORIZON:
                continue
            # 대기
            tw = t + WAIT_STEP
            if free(cell, t, tw - 1, moved):
                ns = (cell, head, tw, True, moved)
                if ns not in parent:
                    parent[ns] = (st, None)
                    counter += 1
                    heapq.heappush(heap, (tw + P.RESTART_TICKS + h(cell, head), -tw, counter, ns))
            # 이동
            dep = t + (P.RESTART_TICKS if stopped else 0)
            for n in self.grid.neighbors(cell):
                d = traverse_ticks(cell, n, head, loaded)
                if not free(cell, t, dep + d - 1, moved) or not tbl.is_free(n, dep, dep + d - 1, agent):
                    continue
                nd = (n[0] - cell[0], n[1] - cell[1])
                ns = (n, nd, dep + d, False, True)
                if ns not in parent:
                    parent[ns] = (st, (dep, n, d))
                    counter += 1
                    heapq.heappush(heap, (dep + d + h(n, nd), -(dep + d), counter, ns))

        if found is None:
            self.park(agent, start, now)       # 제자리 점유 유지
            return None
        if conflict:
            self.predicted_conflicts += 1
        moves = []
        st = found
        while parent[st] is not None:
            prev, mv = parent[st]
            if mv is not None:
                moves.append(mv)
            st = prev
        moves.reverse()
        if len(moves) > len(static_path(start, goal)) or (moves and moves[-1][0] + moves[-1][2] - now >
                                                         _free_flow_ticks(start, goal, heading, loaded)):
            self.detours += 1
        # 예약: 현재 칸 ~ 목적지(무기한)
        cur, cur_start = start, now
        for dep, n, d in moves:
            tbl.reserve(cur, cur_start, dep + d - 1, agent)
            cur, cur_start = n, dep
        tbl.reserve(cur, cur_start, INF, agent)
        return moves


def _free_flow_ticks(start, goal, heading, loaded) -> int:
    total, cur, head = 0, start, heading
    for n in static_path(start, goal):
        total += traverse_ticks(cur, n, head, loaded)
        head, cur = (n[0] - cur[0], n[1] - cur[1]), n
    return total
