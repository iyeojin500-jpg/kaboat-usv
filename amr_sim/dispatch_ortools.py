"""[레이어 2] OR-Tools Routing(VRP) 작업배정·순서 최적화 (CASE2).

롤링 호라이즌 방식:
  배정 시점마다 (1) 대기 중인 가장 오래된 주문 최대 K 건과 (2) AMR 3대(바쁜 AMR 는 현재 작업이 끝나는
  위치·예상시각에서 출발)를 VRP 로 풀고, **지금 비어 있는 AMR** 의 경로 첫 주문만 확정한다.
  나머지는 다음 배정 시점에 새 정보(새 주문, AMR 위치)로 다시 푼다.

비용 (단위 tick = 0.1초, 모두 [레이어 1] A* 자유주행 시간으로 계산):
  - 호(arc) 비용 = 공차(무적재) 이동시간: AMR 위치(또는 이전 주문의 출고장) → 입고장
  - 시간 차원    = 공차 이동 + 주문 처리시간(적재 → RFID → 저장구역 적치·피킹 → 출고장 하역, 저장구역마다 다름)
  - 목적함수     = Σ 공차 이동시간 + Σ (주문별 가중치 × 주문 착수시각)
                  가중치는 오래 기다린 주문일수록 커져서(에이징) 먼 주문이 계속 밀리는 것을 막는다.
"""
from __future__ import annotations

from ortools.constraint_solver import pywrapcp, routing_enums_pb2

from . import params as P
from .flow import service_ticks
from .layout import INBOUND_STATION, OUTBOUND_STATION
from .motion import path_ticks

K_CANDIDATES = 12
CONGESTION_PENALTY = 100         # tick: 다른 AMR 가 향하는/작업 중인 칸 근처로 보내는 배정에 가산
AGE_WEIGHT_PER_TICKS = 1200      # 120초 기다릴 때마다 착수시각 가중치 +1
SOLVE_LIMIT_MS = 200


class ORToolsDispatcher:
    name = "OR-Tools VRP"

    def __init__(self, k: int = K_CANDIDATES, congestion: int = CONGESTION_PENALTY):
        self.k = k
        self.congestion = congestion
        self.solves = 0

    def assign(self, sim, now: int, queue, available):
        cands = sorted(queue, key=lambda j: (j.release, j.job_id))[: self.k]
        amrs = sim.amrs
        free_ids = {a.idx for a in available}
        starts = [sim.projected_state(a) for a in amrs]          # (위치, 가능 시각 tick)
        V, N = len(amrs), len(cands)
        end = V + N                                               # 공통 도착 더미 노드
        svc = [0] * V + [service_ticks(j) for j in cands] + [0]

        def loc_out(i):   # 노드를 떠날 때 위치
            return starts[i][0] if i < V else OUTBOUND_STATION

        def travel(i, j):
            if i == end or j == end or j < V:
                return 0
            return path_ticks(loc_out(i), INBOUND_STATION, False)

        size = V + N + 1
        tr = [[travel(i, j) for j in range(size)] for i in range(size)]

        # 혼잡 회피: 다른 AMR 가 아직 저장구역 처리를 마치지 않은 같은 저장구역(랙)의 주문을
        # 빈 AMR 의 첫 작업으로 주면 그 랙 앞에서 대기가 생기므로 비용을 더한다.
        hot = {a.job.rack for a in amrs if a.job is not None and a.stage in ("TO_IN", "AT_IN", "TO_RFID", "AT_RFID", "TO_RACK", "AT_RACK")}

        def congested(job):
            return job.rack in hot

        if self.congestion:
            for n, job in enumerate(cands):
                if congested(job):
                    for v in range(V):
                        if v in free_ids:
                            tr[v][V + n] += self.congestion

        mgr = pywrapcp.RoutingIndexManager(size, V, list(range(V)), [end] * V)
        routing = pywrapcp.RoutingModel(mgr)

        def arc_cb(fi, ti):
            return tr[mgr.IndexToNode(fi)][mgr.IndexToNode(ti)]

        def time_cb(fi, ti):
            i, j = mgr.IndexToNode(fi), mgr.IndexToNode(ti)
            return svc[i] + tr[i][j]

        arc_idx = routing.RegisterTransitCallback(arc_cb)
        routing.SetArcCostEvaluatorOfAllVehicles(arc_idx)
        time_idx = routing.RegisterTransitCallback(time_cb)
        horizon = 10 ** 7
        routing.AddDimension(time_idx, 0, horizon, False, "Time")
        tdim = routing.GetDimensionOrDie("Time")
        for v in range(V):
            offset = max(0, starts[v][1] - now)
            tdim.CumulVar(routing.Start(v)).SetRange(offset, offset)
        for n in range(N):
            job = cands[n]
            age = max(0, now - P.ticks(job.release))
            w = 1 + age // AGE_WEIGHT_PER_TICKS
            tdim.SetCumulVarSoftUpperBound(mgr.NodeToIndex(V + n), 0, w)

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
            if v not in free_ids:
                continue
            nxt = mgr.IndexToNode(sol.Value(routing.NextVar(routing.Start(v))))
            if V <= nxt < end:
                pairs.append((cands[nxt - V], amrs[v]))
        return pairs
