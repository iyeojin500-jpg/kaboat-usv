"""CASE 구성: 엔진 + (배정, 경로) 부품 조합."""
from __future__ import annotations

from . import params as P
from .dispatch_ortools import ORToolsDispatcher
from .engine import Simulation
from .layout import build_grid
from .reservation import CooperativePlanner
from .strategies import IndependentPlanner, SimpleDispatcher


def make_sim(case: str, jobs, n_amr: int = P.AMR_COUNT, disturbances=(), seed: int = P.SEED) -> Simulation:
    """case: "CASE1" | "CASE2" | "ORTOOLS_ONLY" | "COOP_ONLY" (뒤의 둘은 레이어별 기여도 확인용).
    ORTOOLS_ONLY = 후속 작업 선택·연속 수행만 / COOP_ONLY = 예약테이블 경로만."""
    grid = build_grid()
    # (배정, 경로, 작업 완료 후 연속 수행 여부)
    #   CASE1: 단순 순차 + 독립 A* + 작업마다 작업장(입고장) 복귀
    #   CASE2: OR-Tools 후속 작업 선택 + Cooperative A*/예약테이블 + 연속 수행(복귀는 선택)
    dispatcher, planner, chaining = {
        "CASE1": (SimpleDispatcher, IndependentPlanner, False),
        "CASE2": (ORToolsDispatcher, CooperativePlanner, True),
        "ORTOOLS_ONLY": (ORToolsDispatcher, IndependentPlanner, True),
        "COOP_ONLY": (SimpleDispatcher, CooperativePlanner, False),
    }[case]
    return Simulation(case, jobs, dispatcher(), planner(grid), n_amr=n_amr, grid=grid,
                      disturbances=disturbances, seed=seed, chaining=chaining)
