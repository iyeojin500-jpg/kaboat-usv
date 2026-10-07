# kaboat-usv — AMR 3대 · 주문 1,000건 자동화 창고 시뮬레이션

실험설계: [`docs/new_scenario.md`](docs/new_scenario.md)

**Before = CASE1** (A*만: 단순 순차 배정 + 독립 경로, 충돌 시 대기, 작업마다 작업장(입고장) 복귀) vs
**After = CASE2** (OR-Tools VRP 후속 작업 선택 + 연속 수행(복귀는 선택) + Cooperative A* + 예약테이블) 를
창고·주문 1,000건·작업시간·RFID 결과·작업자 수가 완전히 같은 조건(seed 42)으로 비교한다.

```bash
pip install -r requirements.txt          # ortools, pygame, openpyxl
python run.py                            # 헤드리스: Before/After 실행 → results/ 에 CSV·결과표 (약 40초)
python run.py --seeds 1 2 3 7            # 여러 seed 반복실험 → results/seeds_summary.csv
python run.py --ablation                 # 레이어별 기여도 (OR-Tools만 / Cooperative A*만)
python run.py --gap-scale 3              # 민감도: 주문 발생 간격 3배 (부하 낮춤)
python visualize.py                      # 시각화: 두 CASE 나란히 재생 (pygame 창)
python visualize.py --start 4000         # 피크1 직전부터
```

## 주문 1건 = 작업 2개
- **입고 작업**: 입고장(15,19) 적재 → RFID 구역(15,17) 인식 → 저장구역 A~J 빈 슬롯에 적치
- **출고 작업**: 저장구역에서 실제 화물(FIFO) 피킹 → 중앙 교차로 → 좁은 통로(15,2→1, 1대씩) → 출고장(15,0) 하역
- 선행조건: 같은 주문의 입고 작업이 끝나야 출고 작업 생성. 주문 완료 = 출고 작업 완료.
- 작업 완료 후: **CASE1** 은 반드시 작업장(입고장)으로 복귀한 뒤 다음 작업을 받는다.
  **CASE2** 는 그 자리에서 후속 작업 후보를 평가해 바로 이어서 수행한다 (입고장 복귀는 입고 작업을 고를 때만 생기는 선택).
  후속 작업 후보 조건(존재·미완료·미배정·선행조건·실제 화물/빈 슬롯)은 `engine.task_feasible`, 선택은 `dispatch_ortools.py`.

`python -m amr_sim.layout` 으로 격자 확인.

## 측정 (판정 기준 = docs/new_scenario.md 4장, 코드: `amr_sim/problems.py`)
| 문제 | 코드 판정 | 카운트 |
|---|---|---|
| 경로 충돌 | 막힌 AMR 가 들어가려는 칸으로 다른 AMR 가 같은 시각에 진입 중 | 같은 AMR 쌍·같은 칸이 tick 마다 이어지면 1회 |
| 정면 충돌 | 두 AMR 가 서로의 현재 칸으로 가려다 둘 다 멈춤 | 같은 쌍·같은 두 칸 연속이면 1회 |
| 병목 | 공용구역(RFID·중앙교차로·좁은통로·출고진입부) 밖에서 구역 칸 진입을 다른 AMR 때문에 대기 | 대기 시작~진입까지 1회 |
| 배차 비효율 | 배정 AMR 예상 수행비용 > 그 시점 운용 AMR 중 최소 비용 | 주문당 최대 1회 |
| 장시간 정체 개입 | 대기 상태가 연속 5초 초과 | 5초 넘는 순간 1회, 개입시간 = 5초 이후 정체 시간 |
| 수동 배차 확인 개입 | 배정 AMR 비용 ≥ 최소 비용 × 1.2 | 주문당 최대 1회 (1회당 10초 투입 가정) |

- 예상 수행비용 = (AMR 가 현재 주문을 끝낼 때까지 남은 시간) + (입고장까지 공차 이동) + (주문 처리 예상시간), 자유주행 기준
- 재고 정확도 = WMS 기록이 실물과 일치한 재고 기록 / 전체 재고 기록 (초기 150 + 적치 1,000, 누적).
  RFID 오인식(0.5%, 미감지)이 불일치를 만든다. 인식 실패(1%, 감지)는 작업자가 10초 개입해 바로잡는다.
- 설비 가동률 = (이동 + 작업 시간) / 전체 시간(makespan) × 100, AMR 평균

## 산출물 (`results/`)
| 파일 | 내용 |
|---|---|
| `case1_orders.csv`, `case2_orders.csv` | **주문별**: 발생·작업시작·입고장·RFID·저장구역·출고 시각, 총처리시간, 대응시간, 배차비용/최소비용/추가거리·시간, 문제·개입 횟수 |
| `case1_amr.csv`, `case2_amr.csv` | **AMR별**: 이동거리, 이동/작업/대기/유휴 시간, 정지 횟수, 가동률, 충돌·병목·개입 관여 |
| `system_summary.csv` | **시스템**: makespan, 처리량, 문제별 횟수·총/평균/최대 시간, 구역별 병목, 사람개입, RFID, 재고 정확도 |
| `case*_events.csv` | 문제 에피소드 원본 (종류·AMR·위치·시작·종료·지속·관련 주문) — 카운트 검증용 |
| `summary.md` | KPI 5종 Before/After/개선율/목표 판정 + 문제점 비교표 |
| `결과정리.xlsx` | 필요 데이터 표(주문·AMR·충돌·병목·배차·시스템·RFID·작업자) CASE1/CASE2/개선율 + 원본 데이터 시트 (`python run.py` 후 `python tools/make_excel.py` 로 생성) |
| `floorplan.png` | 창고 배치도 (`python tools/draw_floorplan.py`) |

## 구조
| 모듈 | 역할 |
|---|---|
| `layout.py` | 30x20 격자, 랙 A~J, 벽·좁은 통로·일방통행, RFID, 공용구역 |
| `pathfinding.py`, `motion.py` | [레이어 1] A* 최단경로·거리/시간 행렬, 회전·교차로 감속 |
| `jobs.py` | 주문 1,000건 생성, 실물/WMS 재고 모델 |
| `flow.py` | 주문 1건 흐름과 예상시간 (배정·배차비용 판정 공용) |
| `strategies.py` | CASE1 부품: 단순 순차 배정, 독립 A* |
| `dispatch_ortools.py` | CASE2 [레이어 2] OR-Tools VRP 롤링 호라이즌 배정 |
| `reservation.py` | CASE2 Cooperative A* + 예약테이블 |
| `engine.py` | 공통 시뮬레이션 엔진 (0.1초 tick) |
| `problems.py` | 문제·사람개입 판정 |
| `metrics.py` | KPI·CSV |
| `disturbances.py` | **방해요소 플러그인 훅** (재고부족·AMR고장 자리 마련) |
| `cases.py` | CASE 조합: `make_sim(case, orders, disturbances=[...])` |

## 방해요소 추가 방법
`amr_sim/disturbances.py` 의 `Disturbance` 를 상속해 필요한 훅만 구현하고 `make_sim(..., disturbances=[...])` 로 넣는다.
| 훅 | 호출 시점 | 용도 예 |
|---|---|---|
| `on_tick(sim)` | 매 tick 시작 | 고장 발생/복구 판정 |
| `amr_available(sim, amr)` | 배정 시 | 고장·충전 중 AMR 배정 제외 |
| `amr_can_move(sim, amr)` | AMR 이동 전 | 고장 AMR 정지 (칸 점유 유지 → 다른 AMR 우회·대기) |
| `rack_tasks(sim, amr, order)` | 저장구역 도착 | 재고부족 확인·보충 대기 작업 추가 |
| `on_order_done(sim, amr, order)` | 주문 완료 | 통계 |
| `report()` | 결과 집계 | 시스템 요약 CSV 에 항목 추가 |

`StockShortage`, `AMRBreakdown` 클래스 자리와 구현 메모가 들어 있다. AMR 고장 시간은 AMR별 CSV 의 `고장(s)` 열에 이미 집계된다.

## 가정값
`amr_sim/params.py` 한 곳에 모음: AMR 1.5/1.2 m/s, 회전 0.6, 교차로 0.8, 적재·적치·피킹·하역 10초 ±30%,
RFID 태그 5초·인식 1초·WMS 1초, 인식 실패 1%·오인식 0.5%, 작업자 개입 10초, 판정 기준 5초·20%. 1 tick = 0.1초.
