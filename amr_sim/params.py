"""동작 시간·속도 가정값 (설계 문서 5장). 실장비 선정 시 이 파일만 교체한다.

작업시간은 평균 중심 ±JITTER 범위의 균등분포로 뽑는다 (예: 10초 → 7~13초).
WMS 처리·RFID 자동인식·재출발 지연은 고정값으로 둔다.
AMR 시간당 처리량은 어디에도 넣지 않는다 — 결과로 산출된다.
"""
from __future__ import annotations

import random

SEED = 42
JITTER = 0.3

# ---- AMR (CASE2)
AMR_SPEED_EMPTY = 1.5     # m/s
AMR_SPEED_LOADED = 1.2
AMR_SPEED_TURN = 0.6      # 방향 전환이 일어나는 셀
AMR_SPEED_CROSS = 0.8     # 교차로 셀
AMR_RESTART_DELAY = 1.0   # 정지 후 재출발 지연
AMR_PICK = 10.0           # 로봇팔 피킹 (랙)
AMR_PLACE = 10.0          # 로봇팔 적치 (랙)
AMR_LOAD = 10.0           # 입고장 적재
AMR_UNLOAD = 10.0         # 출고장 하역
AMR_COUNT = 3

# ---- 사람 (CASE1)
HUMAN_SPEED = 1.2
HUMAN_PICK = 10.0
HUMAN_PLACE = 10.0
CART_LOAD = 5.0
CART_UNLOAD = 5.0
BARCODE_SCAN = 3.0
WMS_PROCESS = 1.0
CASE1_WORKERS = 4

# ---- RFID (CASE2)
RFID_TAG = 5.0            # 사람이 태그 부착
RFID_READ = 1.0           # 자동 인식
RFID_WMS = 1.0            # WMS 업데이트
CASE2_WORKERS = 2         # 입고장 1 (태그 부착), 출고장 1 (포장·확인)


def jitter(rng: random.Random, mean: float) -> float:
    return rng.uniform(mean * (1 - JITTER), mean * (1 + JITTER))
