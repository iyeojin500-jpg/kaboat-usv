# kaboat-usv — 중소형 부품 물류창고 AMR 시뮬레이션 (CASE1 · CASE2)

순수 파이썬 3.10+ (외부 라이브러리 없음).

```bash
python3 run.py                          # 작업 1,000건 생성 → CASE1·CASE2 실행 → results/ 저장
python3 run.py --snapshot 4300 12500    # CASE2 해당 시각(초)의 격자 화면 출력
python3 run.py --amr 5                  # 민감도: AMR 대수 변경 (CASE1 작업자 수는 --workers)
python3 run.py --no-spatial             # Peak 구간 C·D·H·I 집중(공간적 병목) 끄기
python3 -m amr_sim.layout               # 격자·랙 좌표만 출력
```

## 산출물 (`results/`)
| 파일 | 내용 |
|---|---|
| `jobs.csv` | 공통 입력 작업 1,000건 (seed=42) |
| `case1_jobs.csv`, `case2_jobs.csv` | JOB 별 기록 (발생·배정·시작·완료시각, 대기/이동/피킹·적치/RFID·바코드/병목대기시간, 이동거리, 담당) |
| `summary.md`, `summary.csv` | KPI 결과표 + 개선율, 자원(작업자/AMR)별 처리량 |

## 구조
| 모듈 | 역할 |
|---|---|
| `amr_sim/layout.py` | 30x20 격자, 랙 A~J(장애물), 스테이션, 슬롯 접근 셀, 교차로 |
| `amr_sim/pathfinding.py` | A* (최단거리 중 회전 최소) |
| `amr_sim/jobs.py` | WMS 재고 모델, 작업 생성 (시간적·공간적 병목) |
| `amr_sim/params.py` | 속도·작업시간 가정값 (실장비 선정 시 여기만 교체) |
| `amr_sim/strategies.py` | **작업배정 / 경로계산 전략** — CASE3 는 여기 두 클래스만 교체 |
| `amr_sim/case1.py` | 작업자 4명, 가장 먼저 비는 작업자 배정 (이벤트 기반) |
| `amr_sim/case2.py` | AMR 3대, Δt=0.1s 시간 전진, 셀 점유·고정 우선순위 충돌 처리 |
| `amr_sim/metrics.py` | JOB 기록, KPI |

## 모델링 가정 (설계 문서 외에 정한 것)
- 랙 앞면은 중앙 주 통로를 향함 (A~E 는 y=11, F~J 는 y=9 에서 피킹/적치).
- CASE1: 사람끼리는 충돌 없이 비켜 지나감. 입고 = 스캔·WMS 등록·카트 상차 → 랙 → 카트 하차·적치·WMS 갱신,
  출고 = 피킹·카트 상차 → 출고장 → 카트 하차·바코드 확인·WMS.
- CASE2: 회전하는 셀은 0.6 m/s, 교차로 셀 진입은 0.8 m/s. 다음 셀이 점유돼 있으면 정지(병목대기 시작),
  재출발 시 1초 지연(병목대기에 포함). 막은 AMR 가 멈춰 있으면 2초 후 다른 AMR 셀을 피해 재계획,
  마주 보고 10초 이상 막히면 옆 칸으로 비켜섬 (교착 방지). 작업 없는 AMR 는 창고 모서리 주차 위치로 복귀.
- CASE2 작업자 2명은 입고장(RFID 태그 부착 5초)·출고장에 고정 → 작업자 이동시간 0.
- 작업시간은 평균 ±30% 균등분포 (예: 피킹 7~13초). WMS·RFID 인식·재출발 지연은 고정.
