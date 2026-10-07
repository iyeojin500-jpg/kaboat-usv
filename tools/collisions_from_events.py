"""저장된 문제 에피소드 로그(case*_events.csv)에서 충돌 1건당 작업자 개입시간을 계산 — 시뮬레이션 재실행 없음.

  python tools/collisions_from_events.py            # results/ 기준

- 충돌 = 경로충돌·정면충돌 에피소드 (같은 AMR 쌍·같은 위치가 끊김 없이 이어지면 1건 — 판정 기준 그대로)
- 충돌 1건 = 작업자 개입 1회, 해결시간 20~60초 균등 랜덤 (Random(seed+3), 발생 시각 순, 0.1초 단위)
- 개입시간은 '기록'만 한다: 그 run 의 AMR 이동·처리시간에는 영향을 주지 않는다 (정지 효과 미포함)

쓰는 파일:  case*_collisions.csv,  system_summary.csv 에 충돌·인력·실작업 가동률 열 추가(기존 값은 그대로)
"""
from __future__ import annotations

import argparse
import csv
import os
import random
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from amr_sim import params as P  # noqa: E402

KEYS = ["collision_id", "time", "amr_1", "amr_2", "collision_type", "location", "human_intervention_time", "orders"]


def read(path):
    with open(path, encoding="utf-8-sig") as fh:
        return list(csv.DictReader(fh))


def collisions(res, case, seed):
    ev = [e for e in read(os.path.join(res, f"{case.lower()}_events.csv")) if e["종류"] in ("경로충돌", "정면충돌")]
    ev.sort(key=lambda e: float(e["시작(s)"]))
    rng = random.Random(seed + 3)
    out = []
    for i, e in enumerate(ev, 1):
        amrs = e["AMR"].split("+")
        t = P.ticks(rng.uniform(P.HUMAN_RESOLVE_MIN, P.HUMAN_RESOLVE_MAX)) * P.TICK
        out.append({"collision_id": i, "time": e["시작(s)"], "amr_1": amrs[0], "amr_2": amrs[1] if len(amrs) > 1 else "",
                    "collision_type": e["종류"], "location": e["위치/구역"], "human_intervention_time": round(t, 1),
                    "orders": e["관련 주문"]})
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--results", default="results")
    ap.add_argument("--seed", type=int, default=P.SEED)
    a = ap.parse_args()
    res = a.results
    sys_path = os.path.join(res, "system_summary.csv")
    with open(sys_path, encoding="utf-8-sig") as fh:
        rdr = csv.DictReader(fh)
        header, srows = list(rdr.fieldnames), list(rdr)

    for row in srows:
        case = row["CASE"]
        if not os.path.exists(os.path.join(res, f"{case.lower()}_events.csv")):
            continue
        cols = collisions(res, case, a.seed)
        with open(os.path.join(res, f"{case.lower()}_collisions.csv"), "w", newline="", encoding="utf-8-sig") as fh:
            w = csv.DictWriter(fh, fieldnames=KEYS)
            w.writeheader()
            w.writerows(cols)
        orders = read(os.path.join(res, f"{case.lower()}_orders.csv"))
        amrs = read(os.path.join(res, f"{case.lower()}_amr.csv"))
        makespan = max(float(o["출고완료(s)"]) for o in orders)
        work = sum(float(x["작업시간(s)"]) for x in amrs)
        human = sum(c["human_intervention_time"] for c in cols)
        old_total = float(row.get("작업자 투입시간(s)") or 0) - float(row.get("작업자 투입시간[충돌 해결](s)") or 0)
        add = {
            "설비 가동률 — 실작업(%)": f"{100.0 * work / (len(amrs) * makespan):.3f}",
            "설비 가동률 — 기존 정의(이동+작업, %)": row.get("설비 가동률(%)", row.get("설비 가동률 — 기존 정의(이동+작업, %)", "")),
            "충돌 건수(경로+정면)": len(cols),
            "충돌 해결 작업자 개입 횟수": len(cols),
            "충돌 해결 작업자 개입시간(s)": f"{human:.1f}",
            "충돌 해결 개입 방식": "시간 기록만 (저장된 충돌 기록으로 사후 계산, AMR 정지 없음)",
            "충돌 해결 대기로 AMR 정지한 시간(s)": "0.0",
            "충돌 외 사람개입 합계(정체+배차, 참고)": row.get("사람 개입 합계(정체+배차)",
                                                   row.get("충돌 외 사람개입 합계(정체+배차, 참고)", "")),
            "작업자 투입시간[충돌 해결](s)": f"{human:.1f}",
            "작업자 투입시간(s)": f"{old_total + human:.1f}",
        }
        row.update({k: str(v) for k, v in add.items()})
        for k in add:
            if k not in header:
                header.append(k)
        print(f"{case}: 충돌 {len(cols)}건 → 작업자 개입시간 {human:,.1f}초")

    with open(sys_path, "w", newline="", encoding="utf-8-sig") as fh:
        w = csv.DictWriter(fh, fieldnames=header)
        w.writeheader()
        w.writerows(srows)
    print("갱신: case*_collisions.csv, system_summary.csv")


if __name__ == "__main__":
    main()
