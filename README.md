# kaboat-usv — 부품 물류창고 AMR 시뮬레이션 (v2: CASE1 vs CASE2)

AMR 3대 창고에서 **CASE1 (A*만: 단순 순차 배정 + 독립 경로, 충돌 시 대기)** 과
**CASE2 (A* 거리행렬 → OR-Tools VRP 배정 + Cooperative A*·예약테이블 경로)** 를
창고·주문 1,000건·작업시간·RFID 결과·작업자 수가 **완전히 같은 조건**으로 돌려 비교한다.

```bash
pip install -r requirements.txt          # ortools, pygame
python3 run.py                           # 헤드리스: 두 CASE 실행 → results/ 에 로그·결과표
python3 run.py --ablation                # + 레이어별 기여도 (OR-Tools만 / Cooperative A*만)
python3 visualize.py                     # 시각화: 두 CASE 나란히 재생 (pygame 창)
python3 visualize.py --start 4000        # 피크1 직전부터 재생
```
시각화 조작: SPACE 일시정지 · ↑/↓ 배속 · → 1초 진행 · S 스크린샷 · ESC 종료.
빨간 테두리 = 다른 AMR 때문에 대기 중, 노란 사각형 = 토트 적재, 선 = 앞으로 갈 경로, X = 목적지, 주황 음영 = 병목구간.

## 레이어 구조
```
[레이어 1] A* (pathfinding.py, motion.py) — 지점 간 최단경로·거리/시간 행렬
   ├─ CASE1: SimpleDispatcher (먼저 비는 AMR ← 먼저 온 주문) + IndependentPlanner (각자 A*, 막히면 정지·대기)
   └─ CASE2: ORToolsDispatcher [레이어 2] (dispatch_ortools.py) + CooperativePlanner (reservation.py)
```
- **OR-Tools VRP (롤링 호라이즌)**: 배정 시점마다 대기 주문 최대 12건 × AMR 3대를 VRP 로 풀고, 지금 비어 있는
  AMR 의 첫 주문만 확정. 목적함수 = 공차 이동시간 + Σ(대기 가중치 × 착수시각) + 혼잡 벌점
  (다른 AMR 목적지 근처로 보내면 +10초). 가중치는 오래 기다린 주문일수록 커짐(에이징).
- **Cooperative A* + 예약테이블**: (칸, 시각) 구간을 예약하는 시공간 A*. 휴리스틱은 회전·교차로 감속을 포함한
  자유주행 최소시간(역방향 다익스트라). 목적지를 다른 AMR 가 작업 중이면 작업 종료 예상 시각에 맞춰 도착하도록 계획.
  서로의 목적지를 막는 교착은 한쪽이 잠시 비켜서서 해소.
- 두 CASE 모두 엔진(engine.py)이 칸 단독 점유를 강제 → 물리적 충돌은 0, 1000건 완주 보장.

## 산출물 (`results/`)
| 파일 | 내용 |
|---|---|
| `jobs.csv` | 공통 주문 1,000건 (seed=42, 작업시간·RFID 실패 포함) |
| `case1_A_orders.csv`, `case2_A_orders.csv` | **A. 주문별 로그** — 발생·배정·완료시각, 리드타임, 배정 AMR, 배정 당시 AMR-작업 거리 |
| `case1_B_amr.csv`, `case2_B_amr.csv` | **B. AMR별 요약** — 이동거리·이동/작업/대기/유휴시간, 정지 횟수 |
| `C_system_summary.csv` | **C. 시스템 요약** — makespan, 처리량, 충돌 예상/회피 정지, 병목 대기, RFID, 작업자 |
| `summary.md` | 결과표 + 개선율 (+ 레이어별 기여도) |

## 지표 정의
- **대기시간**: 작업이 있는데 다른 AMR 때문에 멈춘 시간 (충돌 대기, 예약 대기, 재출발 지연 1초, 경로 재시도)
- **유휴시간**: 배정된 작업 없이 서 있는 시간
- **충돌 예상 횟수**: CASE1 = 다음 칸이 막혀 멈춘 횟수, CASE2 = 계획한 경로가 예약테이블 없이 최단경로로 갔다면 충돌했을 횟수
- **병목구간**: 주 통로 x=11~18 (중앙 교차로 부근) + 입·출고 STATION 반경 2칸
- **RFID**: 인식 실패율 1% 주입 → 실패 시 작업자 개입 10초 (가정값)

## 가정값
`amr_sim/params.py` 한 곳에 모음 (AMR 1.5/1.2 m/s, 회전 0.6, 교차로 0.8, 피킹·적치 10초 ±30%, RFID 5/1/1초,
작업자 2명). 1 tick = 0.1초.
