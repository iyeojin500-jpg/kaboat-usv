"""Before(CASE1) vs After(CASE2) 실험 실행 — 헤드리스 모드.

  python3 run.py                         # seed 42 로 두 CASE 실행 → results/ 에 CSV·결과표
  python3 run.py --seeds 1 2 3 7         # 여러 seed 반복실험 (KPI 개선율 평균·범위)
  python3 run.py --ablation              # 레이어별 기여도: OR-Tools만 / Cooperative A*만
  python3 run.py --snapshot 4300         # 해당 시각(초) 격자 화면(텍스트)
  python3 visualize.py                   # 시각화 모드 (pygame)
"""
from __future__ import annotations

import argparse
import csv
import os
import time

from amr_sim import params as P
from amr_sim.cases import make_sim
from amr_sim.jobs import generate_orders, save_orders_csv
from amr_sim.layout import ZONES, describe
from amr_sim.metrics import SimResult, save_amr_summary, save_events, save_order_log, save_system_summary

# (KPI, 계산식, 방향(+1 높을수록 좋음 / -1 낮을수록 좋음), 목표 판정)
KPI_SPEC = [
    ("작업 처리시간(평균, s)", "출고완료 − 주문발생", -1, "개선율 ≥ 20% (목표 20~30%↓)", lambda imp, a: imp >= 20),
    ("설비 가동률(%)", "(이동+작업)/전체시간×100", +1, "개선율 ≥ 15%↑", lambda imp, a: imp >= 15),
    ("재고 정확도(%)", "일치 기록/전체 기록×100", +1, "After ≥ 98%", lambda imp, a: a >= 98),
    ("사람 개입 횟수", "장시간정체 + 수동배차 개입", -1, "개선율 ≥ 10% (목표 10~15%↓)", lambda imp, a: imp >= 10),
    ("주문 대응시간(평균, s)", "작업시작 − 주문발생", -1, "개선율 ≥ 25%↑", lambda imp, a: imp >= 25),
]

# 문제점 비교 (시스템 요약 키, 낮을수록 좋음 여부)
PROBLEM_ROWS = [
    ("완료시간(makespan,s)", True), ("시간당 처리량(건/h)", False), ("평균 총처리시간(s)", True),
    ("평균 주문 대응시간(s)", True), ("평균 실행시간(s)", True), ("총 이동거리(m)", True), ("총 대기시간(s)", True), ("정지 횟수", True),
    ("경로 충돌 횟수", True), ("경로 충돌 대기시간(s)", True), ("경로 충돌 최대 지속(s)", True),
    ("정면 충돌 횟수", True), ("정면 충돌 대기시간(s)", True), ("정면 충돌 최대 지속(s)", True),
    ("병목 횟수", True), ("병목 대기시간(s)", True), ("병목 평균 지속(s)", True), ("병목 최대 지속(s)", True),
    *[(f"병목[{z}] 횟수", True) for z in ZONES], *[(f"병목[{z}] 대기시간(s)", True) for z in ZONES],
    ("배차 비효율 횟수", True), ("배차 추가 이동거리(m)", True), ("배차 추가 예상시간(s)", True),
    ("장시간 정체 개입 횟수", True), ("장시간 정체 개입시간(s)", True), ("장시간 정체 관련 주문수", True),
    ("수동 배차 확인 개입 횟수", True), ("수동 배차 확인 / 주문 100건", True),
    ("사람 개입 합계(정체+배차)", True), ("최대 대기 주문 수", True),
]


def improvement(before, after, direction):
    if not before:
        return None
    return (after - before) / before * 100 if direction > 0 else (before - after) / before * 100


def fmt(v):
    if v is None:
        return "-"
    if isinstance(v, float):
        return f"{v:,.1f}"
    if isinstance(v, int):
        return f"{v:,}"
    return str(v)


def md_table(header, rows):
    out = ["| " + " | ".join(header) + " |", "|" + "|".join("---" for _ in header) + "|"]
    return "\n".join(out + ["| " + " | ".join(str(c) for c in r) + " |" for r in rows])


def kpi_rows(b: SimResult, a: SimResult):
    kb, ka = b.kpis(), a.kpis()
    rows = []
    for name, formula, direction, goal, ok in KPI_SPEC:
        imp = improvement(kb[name], ka[name], direction)
        rows.append([name, formula, fmt(kb[name]), fmt(ka[name]),
                     "-" if imp is None else f"{imp:+.1f}%", goal,
                     "-" if imp is None and name != "재고 정확도(%)" else ("달성" if ok(imp or 0, ka[name]) else "미달")])
    return rows


def problem_rows(results: dict):
    names = list(results)
    sums = {n: r.system_summary() for n, r in results.items()}
    base = sums[names[0]]
    rows = []
    for key, lower_better in PROBLEM_ROWS:
        row = [key, fmt(base[key])]
        for n in names[1:]:
            v = sums[n][key]
            imp = improvement(base[key], v, -1 if lower_better else +1)
            row += [fmt(v), "-" if imp is None else f"{imp:+.1f}%"]
        rows.append(row)
    return rows


def run_case(case, orders, n_amr, seed, snapshots=()):
    sim = make_sim(case, orders, n_amr, seed=seed)
    snaps, frames = sorted(t * 10 for t in snapshots), []
    t0 = time.time()
    while not sim.finished:
        sim.tick()
        while snaps and snaps[0] <= sim.now:
            snaps.pop(0)
            frames.append(sim.frame())
    print(f"  {case}: {sim.dispatcher.name} + {sim.planner.name} … {time.time() - t0:.1f}s", flush=True)
    return sim.result(), frames


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--seed", type=int, default=P.SEED)
    ap.add_argument("--seeds", type=int, nargs="*", help="반복실험 seed 목록 (지정 시 seed 별 KPI 개선율 요약)")
    ap.add_argument("--out", default="results")
    ap.add_argument("--amr", type=int, default=P.AMR_COUNT)
    ap.add_argument("--no-spatial", action="store_true", help="피크 구간 C·D·H·I 집중(공간적 병목) 끄기")
    ap.add_argument("--gap-scale", type=float, default=1.0, help="주문 발생 간격 배율 (민감도 확인용, 기본 1.0)")
    ap.add_argument("--snapshot", type=float, nargs="*", default=[])
    ap.add_argument("--ablation", action="store_true", help="OR-Tools만 / Cooperative A*만 조합도 실행")
    args = ap.parse_args()
    os.makedirs(args.out, exist_ok=True)

    if args.seeds:
        return run_seeds(args)

    print(describe(), "\n")
    orders = generate_orders(args.seed, spatial_bottleneck=not args.no_spatial, gap_scale=args.gap_scale)
    save_orders_csv(orders, os.path.join(args.out, "orders_input.csv"))
    print(f"주문 {len(orders)}건 (O0001~O{len(orders):04d}), 마지막 발생 {orders[-1].release:,.1f}s, "
          f"RFID 실패 {sum(o.rfid == 'FAIL' for o in orders)}건 · 오인식 {sum(o.rfid == 'MISREAD' for o in orders)}건 (주입)\n실행:")

    cases = ["CASE1", "CASE2"] + (["ORTOOLS_ONLY", "COOP_ONLY"] if args.ablation else [])
    results = {}
    for c in cases:
        res, frames = run_case(c, orders, args.amr, args.seed, args.snapshot)
        results[c] = res
        for f in frames:
            print(f, "\n")
        save_order_log(res, os.path.join(args.out, f"{c.lower()}_orders.csv"))
        save_amr_summary(res, os.path.join(args.out, f"{c.lower()}_amr.csv"))
        save_events(res, os.path.join(args.out, f"{c.lower()}_events.csv"))
    save_system_summary(list(results.values()), os.path.join(args.out, "system_summary.csv"))

    b, a = results["CASE1"], results["CASE2"]
    lines = [
        "# Before(CASE1) vs After(CASE2) 결과", "",
        f"- Before CASE1: {b.dispatcher} + {b.planner}",
        f"- After  CASE2: {a.dispatcher} + {a.planner}",
        f"- 공통: AMR {args.amr}대, 주문 {len(orders)}건, seed={args.seed}, 발생간격 배율 {args.gap_scale:g} "
        f"(동일 주문·작업시간·RFID 결과)",
        f"- 부하: 주문 발생 {len(orders) / (orders[-1].release / 3600):.0f}건/h vs Before 처리능력 "
        f"{b.system_summary()['시간당 처리량(건/h)']:.0f}건/h", "",
        "## 기대효과 KPI", "",
        md_table(["기대효과", "계산식", "Before", "After", "개선율", "목표", "판정"], kpi_rows(b, a)), "",
        "개선율: 낮을수록 좋은 지표 (B−A)/B×100, 높을수록 좋은 지표 (A−B)/B×100. 양수 = After 가 좋음.", "",
        "## 문제점 측정 (판정 기준: new_scenario.md 4장)", "",
        md_table(["지표", "CASE1"] + sum(([n, "개선율"] for n in list(results)[1:]), []), problem_rows(results)), "",
    ]
    for r in results.values():
        rows = r.amr_rows()
        lines += [f"## AMR별 요약 — {r.name}", "",
                  md_table(list(rows[0].keys()), [[fmt(v) for v in x.values()] for x in rows]), ""]
    text = "\n".join(lines)
    print("\n" + text)
    with open(os.path.join(args.out, "summary.md"), "w", encoding="utf-8") as f:
        f.write(text)
    print(f"저장: {args.out}/ — case*_orders.csv(주문별) · case*_amr.csv(AMR별) · system_summary.csv(시스템) "
          f"· case*_events.csv(문제 에피소드) · summary.md")


def run_seeds(args):
    rows, table = [], []
    for seed in args.seeds:
        orders = generate_orders(seed, spatial_bottleneck=not args.no_spatial, gap_scale=args.gap_scale)
        print(f"seed {seed}:")
        b, _ = run_case("CASE1", orders, args.amr, seed)
        a, _ = run_case("CASE2", orders, args.amr, seed)
        kb, ka = b.kpis(), a.kpis()
        row = {"seed": seed}
        for name, _, direction, _, _ in KPI_SPEC:
            row[f"{name} Before"] = kb[name]
            row[f"{name} After"] = ka[name]
            imp = improvement(kb[name], ka[name], direction)
            row[f"{name} 개선율(%)"] = imp
        for key in ("완료시간(makespan,s)", "경로 충돌 횟수", "정면 충돌 횟수", "병목 횟수", "배차 비효율 횟수"):
            row[f"{key} Before"] = b.system_summary()[key]
            row[f"{key} After"] = a.system_summary()[key]
        rows.append(row)
    path = os.path.join(args.out, "seeds_summary.csv")
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)
    print("\n## seed 반복실험 — KPI 개선율(%)")
    header = ["seed"] + [k for k, *_ in KPI_SPEC]
    body = [[r["seed"]] + [fmt(r[f"{k} 개선율(%)"]) for k, *_ in KPI_SPEC] for r in rows]
    avg = ["평균"] + [fmt(sum(v) / len(v)) if (v := [r[f"{k} 개선율(%)"] for r in rows if r[f"{k} 개선율(%)"] is not None]) else "-"
                    for k, *_ in KPI_SPEC]
    print(md_table(header, body + [avg]))
    print(f"저장: {path}")


if __name__ == "__main__":
    main()
