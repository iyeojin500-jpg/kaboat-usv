"""이미 저장된 결과 CSV 만으로 KPI·인력 개입시간을 다시 계산 (시뮬레이션 재실행 없음).

  python tools/kpi_from_csv.py                 # results/ 기준
  python tools/kpi_from_csv.py --results 폴더

읽는 파일:  case1/case2_collisions.csv (충돌 이벤트별 작업자 개입시간), case*_orders.csv, case*_amr.csv, system_summary.csv
쓰는 파일:  simulation_comparison.csv (덮어씀), summary.md 의 '기대효과 KPI' 절 (그 부분만 교체)
"""
from __future__ import annotations

import argparse
import csv
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from run import KPI_SPEC, fmt, improvement, judge, md_table  # noqa: E402

LAB = "인력 의존도 — 충돌 해결 개입시간(s)"


def rows(path):
    with open(path, encoding="utf-8-sig") as fh:
        return list(csv.DictReader(fh))


def case_kpis(res: str, case: str) -> dict:
    c = case.lower()
    orders = rows(os.path.join(res, f"{c}_orders.csv"))
    amrs = rows(os.path.join(res, f"{c}_amr.csv"))
    cols = rows(os.path.join(res, f"{c}_collisions.csv"))
    sysrow = next(r for r in rows(os.path.join(res, "system_summary.csv")) if r["CASE"] == case)
    makespan = max(float(o["출고완료(s)"]) for o in orders)
    work = sum(float(a["작업시간(s)"]) for a in amrs)
    times = [float(x["human_intervention_time"]) for x in cols]
    return {
        "작업 처리시간(평균, s)": sum(float(o["총처리시간(s)"]) for o in orders) / len(orders),
        "설비 가동률 — 실작업(%)": 100.0 * work / (len(amrs) * makespan),
        "재고 정확도(%)": float(sysrow["재고 정확도(%)"]),
        LAB: sum(times),
        "주문 대응시간(평균, s)": sum(float(o["주문 대응시간(s)"]) for o in orders) / len(orders),
        "_count": len(times),
        "_by_type": {t: sum(1 for x in cols if x["collision_type"] == t) for t in ("경로충돌", "정면충돌")},
        "_makespan": makespan,
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--results", default="results")
    a = ap.parse_args()
    b, o = case_kpis(a.results, "CASE1"), case_kpis(a.results, "CASE2")

    row = {
        "baseline_collision_count": b["_count"], "optimized_collision_count": o["_count"],
        "baseline_human_intervention_count": b["_count"], "optimized_human_intervention_count": o["_count"],
        "baseline_human_intervention_time": round(b[LAB], 1), "optimized_human_intervention_time": round(o[LAB], 1),
        "labor_dependency_reduction": round(improvement(b[LAB], o[LAB], -1) or 0.0, 2),
        "labor_dependency_reduction_by_count": round(improvement(b["_count"], o["_count"], -1) or 0.0, 2),
    }
    table = []
    for name, formula, direction, goal, low, high in KPI_SPEC:
        imp = improvement(b[name], o[name], direction)
        verdict = judge(name, imp, o[name], low, high)
        row[f"{name} baseline"] = round(b[name], 2)
        row[f"{name} optimized"] = round(o[name], 2)
        row[f"{name} 개선율(%)"] = None if imp is None else round(imp, 2)
        row[f"{name} 판정"] = verdict
        table.append([name, formula, fmt(b[name]), fmt(o[name]), "-" if imp is None else f"{imp:+.1f}%", goal, verdict])
    with open(os.path.join(a.results, "simulation_comparison.csv"), "w", newline="", encoding="utf-8-sig") as fh:
        w = csv.DictWriter(fh, fieldnames=list(row))
        w.writeheader()
        w.writerow(row)

    section = "\n".join([
        "## 기대효과 KPI", "",
        md_table(["기대효과", "계산식", "Before", "After", "개선율", "목표", "판정"], table), "",
        "개선율: 낮을수록 좋은 지표 (B−A)/B×100, 높을수록 좋은 지표 (A−B)/B×100. 양수 = After 가 좋음.",
        f"인력 의존도: 충돌 해결 작업자 개입 Before {b['_count']}회 / {b[LAB]:,.1f}초 → After {o['_count']}회 / "
        f"{o[LAB]:,.1f}초 (case*_collisions.csv 합계). 개입 횟수 기준 감소율 {row['labor_dependency_reduction_by_count']:+.1f}%.",
        "(이 절은 tools/kpi_from_csv.py 가 저장된 CSV 로 계산 — 시뮬레이션 재실행 없음)", "", ""])
    p = os.path.join(a.results, "summary.md")
    text = open(p, encoding="utf-8").read()
    s, e = text.index("## 기대효과 KPI"), text.index("## 문제점 측정")
    open(p, "w", encoding="utf-8").write(text[:s] + section + text[e:])

    print(md_table(["기대효과", "계산식", "Before", "After", "개선율", "목표", "판정"], table))
    print(f"\n충돌 해결 개입: Before {b['_count']}회 {b['_by_type']} / {b[LAB]:,.1f}초  →  "
          f"After {o['_count']}회 {o['_by_type']} / {o[LAB]:,.1f}초")
    print("갱신: simulation_comparison.csv, summary.md (기대효과 KPI 절)")


if __name__ == "__main__":
    main()
