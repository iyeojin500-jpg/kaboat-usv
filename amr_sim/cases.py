"""CASE 구성: 엔진 + (배정, 경로) 부품 조합."""
from __future__ import annotations

from . import params as P
from .dispatch_ortools import ORToolsDispatcher
from .engine import Simulation
from .layout import build_grid
from .reservation import CooperativePlanner
from .strategies import IndependentPlanner, SimpleDispatcher


def make_sim(case: str, jobs, n_amr: int = P.AMR_COUNT, disturbances=(), seed: int = P.SEED) -> Simulation:
    """case: "CASE1" | "CASE2" | "ORTOOLS_ONLY" | "COOP_ONLY" (뒤의 둘은 레이어별 기여도 확인용)."""
    grid = build_grid()
    dispatcher, planner = {
        "CASE1": (SimpleDispatcher, IndependentPlanner),
        "CASE2": (ORToolsDispatcher, CooperativePlanner),
        "ORTOOLS_ONLY": (ORToolsDispatcher, IndependentPlanner),
        "COOP_ONLY": (SimpleDispatcher, CooperativePlanner),
    }[case]
    return Simulation(case, jobs, dispatcher(), planner(grid), n_amr=n_amr, grid=grid,
                      disturbances=disturbances, seed=seed)
