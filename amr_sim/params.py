"""동작 시간·속도 가정값 (설계 문서 5장). 실장비 선정 시 이 파일만 교체한다.

작업시간(피킹·적치·적재·하역·태그부착)은 평균 ±JITTER 균등분포로, 주문 생성 시 주문마다 미리 뽑아
두 CASE 에 똑같이 적용한다. AMR 시간당 처리량은 어디에도 넣지 않는다 — 결과로 산출된다.

시뮬레이션 시간 단위는 1 tick = 0.1초 (정수). 예약테이블과 실제 실행이 같은 시계를 쓰도록 하기 위함.
"""
from __future__ import annotations

import math
import random

SEED = 42
JITTER = 0.3
TICK = 0.1                 # 초/tick

# ---- AMR
AMR_COUNT = 3
AMR_SPEED_EMPTY = 1.5      # m/s
AMR_SPEED_LOADED = 1.2
AMR_SPEED_TURN = 0.6       # 방향 전환이 일어나는 칸
AMR_SPEED_CROSS = 0.8      # 교차로 칸 진입
AMR_RESTART_DELAY = 1.0    # 정지 후 재출발 지연
AMR_PICK = 10.0            # 랙 피킹 (출고할 토트)
AMR_PLACE = 10.0           # 랙 적치 (입고된 토트)
AMR_LOAD = 10.0            # 입고장 적재
AMR_UNLOAD = 10.0          # 출고장 하역

# ---- RFID · 작업자 (두 CASE 동일 인원)
N_WORKERS = 2              # 입고장 1 (태그 부착·오류대응), 출고장 1 (후처리·오류대응)
RFID_TAG = 5.0             # 사람이 태그 부착
RFID_READ = 1.0            # 자동 인식
RFID_WMS = 1.0             # WMS 갱신
RFID_FAIL_RATE = 0.01      # 인식 실패(읽기 안 됨, 감지됨) → 작업자 수동 확인 후 정상 기록
RFID_MISREAD_RATE = 0.005  # 오인식(다른 ID 로 읽힘, 감지 안 됨) → WMS 기록 불일치 (재고 정확도에 반영)
WORKER_INTERVENTION = 10.0 # 인식 실패 시 작업자 수동 확인·재부착 (가정값)

# ---- 문제·사람개입 판정 기준 (실제 업체 운영기준 확보 시 이 값만 교체)
STALL_INTERVENTION_SEC = 5.0     # 충돌·병목으로 연속 5초 이상 못 움직이면 장시간 정체 개입 1회
DISPATCH_CHECK_RATIO = 0.20      # 배정 AMR 예상비용이 최소 가능 비용보다 20% 이상 크면 수동 배차 확인 개입
DISPATCH_CHECK_SEC = 10.0        # 수동 배차 확인 1회당 작업자 투입시간 (가정값, 운영에는 지연을 주지 않음)


def jitter(rng: random.Random, mean: float) -> float:
    return rng.uniform(mean * (1 - JITTER), mean * (1 + JITTER))


def ticks(seconds: float) -> int:
    return int(math.floor(seconds / TICK + 0.5))


RESTART_TICKS = ticks(AMR_RESTART_DELAY)
