"""[레이어 2] OR-Tools Routing(VRP) 후속 작업 선택·배정 (CASE2).

롤링 호라이즌: 배정 시점마다 대기 작업 중 우선순위가 높은 최대 K 건과 AMR 전부(바쁜 AMR 는 현재 작업이
끝나는 위치·예상시각에서 출발)를 VRP 로 풀고, 지금 비어 있는 AMR 의 경로 첫 작업만 확정한다.

작업(노드)마다 시작 위치와 끝 위치가 다르다 (입고: 입고장 → 저장구역, 출고: 저장구역 → 출고장).
그래서 적치를 막 끝낸 AMR 는 같은/근처 저장구역의 출고 작업을 이동 없이 이어받을 수 있고,
입고장으로 돌아가는 것은 입고 작업을 고를 때만 생기는 '선택'이 된다.

고려 요소 (단위 tick = 0.1초, [레이어 1] A* 자유주행 시간)
  - 이동: 호 비용 = 공차 이동시간 (앞 작업 끝 위치 → 다음 작업 시작 위치)
  - 처리시간: 시간 차원 = 공차 이동 + 작업 처리시간 (저장구역·작업 종류마다 다름)
  - 우선순위: Σ (가중치 × 작업 착수시각), 오래 기다린 작업일수록 가중치↑ (에이징 → 출고 순서/우선순위 유지)
  - 충돌 가능성: 다른 AMR 가 향하는 저장구역 작업이면 벌점
  - Makespan·균형: 시간 차원 global span 비용 → 한 AMR 에 작업이 몰리지 않게
"""
from __future__ import annotations

from ortools.constraint_solver import pywrapcp, routing_enums_pb2

from .flow import service_ticks
from .motion import path_ticks
from . import params as P

K_CANDIDATES = 12
AGE_WEIGHT_PER_TICKS = 1200      # 120초 기다릴 때마다 착수시각 가중치 +1
CONGESTION_PENALTY = 100         # tick: 다른 AMR 가 작업 중인 저장구역과 같은 곳으로 보내는 배정에 가산
SPAN_COEF = 1                    # makespan(가장 늦게 끝나는 AMR) 비용 계수 → 작업량 균형
SOLVE_LIMIT_MS = 200


class ORToolsDispatcher:
    name = "OR-Tools VRP"

    def __init__(self, k: int = K_CANDIDATES, congestion: int = CONGESTION_PENALTY):
        self.k = k
        self.congestion = congestion
        self.solves = 0

    def assign(self, sim, now: int, queue, available):
        cands = list(queue)[: self.k]            # 대기 목록은 우선순위 순으로 정렬되어 있음
        amrs = [a for a in sim.amrs if sim.operational(a)]
        free_ids = {a.idx for a in available}
        starts = [sim.projected_state(a) for a in amrs]          # (위치, 가능 시각 tick)
        V, N = len(amrs), len(cands)
        end = V + N                                               # 공통 도착 더미 노드
        svc = [0] * V + [service_ticks(t) for t in cands] + [0]

        def loc_out(i):
            return starts[i][0] if i < V else cands[i - V].end_cell

        def travel(i, j):
            if i == end or j == end or j < V:
                return 0
            return path_ticks(loc_out(i), cands[j - V].start_cell, False)

        size = V + N + 1
        tr = [[travel(i, j) for j in range(size)] for i in range(size)]

        # 충돌 가능성: 다른 AMR 가 아직 처리 중인 저장구역(랙)의 작업을 빈 AMR 첫 작업으로 주면 벌점
        hot = {a.job.rack for a in amrs if a.job is not None and a.stage not in ("TO_OUT", "AT_OUT")}
        if self.congestion:
            for n, t in enumerate(cands):
                if t.rack in hot:
                    for v in range(V):
                        if amrs[v].idx in free_ids:
                            tr[v][V + n] += self.congestion

        mgr = pywrapcp.RoutingIndexManager(size, V, list(range(V)), [end] * V)
        routing = pywrapcp.RoutingModel(mgr)

        def arc_cb(fi, ti):
            return tr[mgr.IndexToNode(fi)][mgr.IndexToNode(ti)]

        def time_cb(fi, ti):
            i, j = mgr.IndexToNode(fi), mgr.IndexToNode(ti)
            return svc[i] + tr[i][j]

        routing.SetArcCostEvaluatorOfAllVehicles(routing.RegisterTransitCallback(arc_cb))
        time_idx = routing.RegisterTransitCallback(time_cb)
        routing.AddDimension(time_idx, 0, 10 ** 7, False, "Time")
        tdim = routing.GetDimensionOrDie("Time")
        tdim.SetGlobalSpanCostCoefficient(SPAN_COEF)
        for v in range(V):
            offset = max(0, starts[v][1] - now)
            tdim.CumulVar(routing.Start(v)).SetRange(offset, offset)
        for n, t in enumerate(cands):
            age = max(0, now - P.ticks(t.release))
            tdim.SetCumulVarSoftUpperBound(mgr.NodeToIndex(V + n), 0, 1 + age // AGE_WEIGHT_PER_TICKS)

        prm = pywrapcp.DefaultRoutingSearchParameters()
        prm.first_solution_strategy = routing_enums_pb2.FirstSolutionStrategy.PATH_CHEAPEST_ARC
        prm.local_search_metaheuristic = routing_enums_pb2.LocalSearchMetaheuristic.GREEDY_DESCENT
        prm.time_limit.FromMilliseconds(SOLVE_LIMIT_MS)
        sol = routing.SolveWithParameters(prm)
        self.solves += 1
        if sol is None:      # 안전장치: 단순 순차
            return list(zip(cands, sorted(available, key=lambda a: a.idx)))

        pairs = []
        for v in range(V):
            if amrs[v].idx not in free_ids:
                continue
            nxt = mgr.IndexToNode(sol.Value(routing.NextVar(routing.Start(v))))
            if V <= nxt < end:
                pairs.append((cands[nxt - V], amrs[v]))
        return pairs
