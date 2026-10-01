"""문제점·사람개입 판정 (new_scenario.md 4장 기준을 코드로 검사).

매 tick 엔진이 AMR 를 한 번씩 움직인 뒤 ProblemDetector.update() 를 호출한다.
각 AMR 는 이번 tick 에 다음 정보를 남긴다.
  attempt     : 진입하려다 거절된 칸 (없으면 None)
  blocker     : 그 칸 때문에 막은 AMR
  wait_target : 대기 중이면 기다리는 대상 칸 (병목 구역 판정용)
  category    : 이번 tick 분류 (move / work / wait / tag / idle)

[경로 충돌]  막힌 AMR 가 진입하려는 칸으로 다른 AMR 가 '지금 들어가고 있음'(같은 시각 같은 칸 진입 시도)
[정면 충돌]  두 AMR 가 서로의 현재 칸으로 가려다 둘 다 멈춤 (A→B, B→A)
[병목]       공용구역(RFID·중앙교차로·좁은통로·출고진입부) 밖에서 그 구역 칸으로 들어가려다 다른 AMR 때문에 대기
[장시간 정체 개입]  다른 AMR 때문에(대기 분류) 연속 5초 이상 못 움직임 → 5초 넘는 순간 1회
카운트 규칙: 같은 AMR(쌍)·같은 위치의 상태가 tick 마다 이어지면 1회(에피소드). 끊겼다 다시 생기면 새 1회.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from . import params as P
from .layout import CELL_ZONE, ZONES

STALL_TICKS = P.ticks(P.STALL_INTERVENTION_SEC)
ZONE_LOOKAHEAD = 3   # 예약 대기 중일 때 앞으로 몇 칸 안에 공용구역이 있으면 그 구역 앞 대기로 본다


@dataclass
class Episode:
    kind: str                 # 경로충돌 / 정면충돌 / 병목 / 장시간정체
    amrs: tuple
    where: str
    start: int
    last: int
    orders: set = field(default_factory=set)

    @property
    def duration(self) -> int:
        return self.last - self.start + 1


class ProblemDetector:
    def __init__(self):
        self.active: dict[tuple, Episode] = {}
        self.closed: list[Episode] = []
        self._stall_run: dict[int, int] = {}      # AMR idx → 연속 대기 tick

    def _touch(self, key, kind, amrs, where, now, orders):
        ep = self.active.get(key)
        if ep is None or ep.last < now - 1:     # 직전 tick 에 이어지지 않으면 새 에피소드
            if ep is not None:
                self.closed.append(ep)
            ep = Episode(kind, amrs, where, now, now)
            self.active[key] = ep
        ep.last = now
        ep.orders.update(o for o in orders if o)

    def update(self, sim):
        now = sim.now
        for a in sim.amrs:
            oid = a.job.order_id if a.job else None
            b = a.blocker
            # --- 경로 충돌 / 정면 충돌
            if a.attempt is not None and b is not None:
                X = a.attempt
                b_oid = b.job.order_id if b.job else None
                pair = tuple(sorted((a.name, b.name)))
                if b.next_cell == X:
                    self._touch(("경로충돌", pair, X), "경로충돌", pair, str(X), now, (oid, b_oid))
                elif b.next_cell is None and b.cell == X and b.attempt == a.cell:
                    cells = tuple(sorted((a.cell, X)))
                    self._touch(("정면충돌", pair, cells), "정면충돌", pair, f"{cells[0]}<->{cells[1]}", now, (oid, b_oid))
            # --- 병목 (구역 밖에서 구역 진입 대기)
            if a.category == "wait" and a.wait_target is not None:
                z = CELL_ZONE.get(a.wait_target)
                if z is not None and CELL_ZONE.get(a.cell) != z:
                    self._touch(("병목", a.name, z), "병목", (a.name,), z, now, (oid,))
            # --- 장시간 정체 (사람 개입)
            if a.category == "wait":
                run = self._stall_run.get(a.idx, 0) + 1
                self._stall_run[a.idx] = run
                if run > STALL_TICKS:
                    key = ("장시간정체", a.name, now - run + 1)
                    self._touch(key, "장시간정체", (a.name,), str(a.cell), now, (oid,))
            else:
                self._stall_run[a.idx] = 0
        # 이번 tick 에 이어지지 않은 에피소드 종료
        for key in [k for k, ep in self.active.items() if ep.last < now]:
            self.closed.append(self.active.pop(key))

    def finish(self):
        self.closed.extend(self.active.values())
        self.active.clear()
        return self.closed


def summarize(episodes: list[Episode], kind: str) -> dict:
    eps = [e for e in episodes if e.kind == kind]
    durs = [e.duration * P.TICK for e in eps]
    return {
        "횟수": len(eps),
        "총시간(s)": sum(durs),
        "평균(s)": sum(durs) / len(durs) if durs else 0.0,
        "최대(s)": max(durs) if durs else 0.0,
        "관련 주문수": len(set().union(*[e.orders for e in eps])) if eps else 0,
    }


def zone_breakdown(episodes: list[Episode]) -> dict:
    out = {}
    for z in ZONES:
        eps = [e for e in episodes if e.kind == "병목" and e.where == z]
        durs = [e.duration * P.TICK for e in eps]
        out[z] = {"횟수": len(eps), "총시간(s)": sum(durs), "평균(s)": sum(durs) / len(durs) if durs else 0.0,
                  "최대(s)": max(durs) if durs else 0.0}
    return out
