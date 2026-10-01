"""방해요소(재고부족·AMR 고장 등) 플러그인 구조.

엔진은 아래 훅을 정해진 시점에 호출만 한다. 새 방해요소는 Disturbance 를 상속해 필요한 훅만
구현하고 make_sim(..., disturbances=[MyDisturbance(...)]) 으로 끼우면 된다.
CASE1·CASE2 에 같은 방해요소 객체 설정(같은 seed)을 넣어야 공정 비교가 된다.

훅 (모두 선택 구현)
-------------------
  attach(sim)                         시뮬레이션 시작 시 1회
  on_tick(sim)                        매 tick 시작 (고장 발생/복구 시각 판정 등)
  amr_available(sim, amr) -> bool     False 면 이 AMR 에 새 주문을 배정하지 않음 (고장·충전 등)
  amr_can_move(sim, amr) -> bool      False 면 이번 tick 정지 — 분류 'down', 칸은 계속 점유
  rack_tasks(sim, amr, order) -> list 저장구역 도착 시 추가 작업 [(이름, tick), ...] (재고부족 확인·보충 대기 등)
  on_order_done(sim, amr, order)      주문 완료 시
  report() -> dict                    시스템 요약 CSV 에 덧붙일 항목 {"이름": 값}
"""
from __future__ import annotations


class Disturbance:
    name = "base"

    def attach(self, sim):
        self.sim = sim

    def on_tick(self, sim):
        pass

    def amr_available(self, sim, amr) -> bool:
        return True

    def amr_can_move(self, sim, amr) -> bool:
        return True

    def rack_tasks(self, sim, amr, order) -> list:
        return []

    def on_order_done(self, sim, amr, order):
        pass

    def report(self) -> dict:
        return {}


class StockShortage(Disturbance):
    """(예정) 재고부족: 저장구역 도착 시 출고 토트가 없으면 보충 대기 / 다른 랙 재배정.

    구현 메모
      - 주문 생성 시 별도 rng 로 부족 여부를 미리 뽑아(seed 고정) 두 CASE 에 동일 적용
      - rack_tasks() 에서 [("STOCK_WAIT", 보충시간 tick)] 반환 + 작업자 개입 카운트
      - jobs.Inventory.has_stock() 으로 실제 재고 기준 판정도 가능
    """
    name = "재고부족"

    def __init__(self, *args, **kwargs):
        raise NotImplementedError("다음 단계에서 구현 예정 — 훅 구조만 준비됨")


class AMRBreakdown(Disturbance):
    """(예정) AMR 고장: 정해진 시각/확률로 AMR 가 멈추고 일정 시간 후 복구.

    구현 메모
      - on_tick() 에서 고장 시작/복구 판정 (고장 일정은 seed 고정으로 미리 생성)
      - amr_can_move() False → 그 자리에서 정지(칸 점유 유지 → 다른 AMR 우회·대기 발생)
      - amr_available() False → 새 배정 제외. 수행 중 주문의 재배정 여부는 정책으로 선택
      - CASE2 는 고장 AMR 의 예약을 무기한 점유로 바꾸면(planner.park) 다른 AMR 가 피해 간다
    """
    name = "AMR고장"

    def __init__(self, *args, **kwargs):
        raise NotImplementedError("다음 단계에서 구현 예정 — 훅 구조만 준비됨")
