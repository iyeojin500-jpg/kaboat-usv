"""CASE1 vs CASE2 시뮬레이션 실행 (헤드리스 모드).

  python3 run.py                         # 기본 실행 → results/ 에 A·B·C 로그와 결과표 저장
  python3 run.py --snapshot 4300 12500   # 해당 시각(초)의 격자 화면(텍스트) 출력
  python3 run.py --ablation              # 레이어별 기여도: OR-Tools만 / Cooperative A*만 추가 실행
  python3 visualize.py                   # 시각화 모드 (pygame)
"""
from __future__ import annotations

import argparse
import os
import time

from amr_sim import params as P
from amr_sim.cases import make_sim
from amr_sim.jobs import generate_jobs, save_jobs_csv
from amr_sim.layout import describe
from amr_sim.metrics import SimResult, save_amr_summary, save_order_log, save_system_summary

# (지표, 키, 단위, 높을수록 좋은가)
COMPARE = [
    ("완료시간(makespan)", "완료시간(makespan,s)", "s", False),
    ("평균 리드타임", "평균 리드타임(s)", "s", False),
    ("총 이동거리", "총 이동거리(m)", "m", False),
    ("총 대기시간", "총 대기시간(s)", "s", False),
    ("정지 횟수(충돌 회피 정지)", "충돌 회피 정지 횟수", "회", False),
    ("병목구간 대기시간", "병목구간 대기시간(s)", "s", False),
    ("시간당 처리량", "시간당 처리량(건/h)", "건/h", True),
    ("충돌 예상 횟수", "충돌 예상 횟수", "회", False),
    ("최대 대기 주문 수", "최대 대기 주문 수", "건", False),
    ("총 유휴시간", "총 유휴시간(s)", "s", False),
]


def fmt(v, unit):
    if unit == "s":
        return f"{v:,.1f}s ({int(v // 3600)}h{int(v % 3600 // 60):02d}m)" if v >= 3600 else f"{v:,.1f}s"
    if unit in ("m", "회", "건"):
        return f"{v:,.0f}{unit}"
    return f"{v:,.1f}{unit}"


def improvement(c1, c2, higher_better):
    if not c1:
        return "-"
    return f"{((c2 - c1) if higher_better else (c1 - c2)) / c1 * 100:+.1f}%"


def md_table(header, rows):
    out = ["| " + " | ".join(header) + " |", "|" + "|".join("---" for _ in header) + "|"]
    return "\n".join(out + ["| " + " | ".join(str(c) for c in r) + " |" for r in rows])


def compare_table(base: SimResult, others: list[SimResult]) -> str:
    s0 = base.system_summary()
    header = ["지표", base.name] + sum(([o.name, "개선율"] for o in others), [])
    rows = []
    for label, key, unit, hb in COMPARE:
        row = [label, fmt(s0[key], unit)]
        for o in others:
            v = o.system_summary()[key]
            row += [fmt(v, unit), improvement(s0[key], v, hb)]
        rows.append(row)
    return md_table(header, rows)


def run_case(case, jobs, n_amr, snapshots):
    sim = make_sim(case, jobs, n_amr)
    snaps, frames = sorted(t * 10 for t in snapshots), []
    t0 = time.time()
    while not sim.finished:
        sim.tick()
        while snaps and snaps[0] <= sim.now:
            snaps.pop(0)
            frames.append(sim.frame())
    print(f"  {case}: {sim.dispatcher.name} + {sim.planner.name} … {time.time() - t0:.1f}s")
    return sim.result(), frames


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--seed", type=int, default=P.SEED)
    ap.add_argument("--out", default="results")
    ap.add_argument("--amr", type=int, default=P.AMR_COUNT)
    ap.add_argument("--no-spatial", action="store_true", help="Peak 구간 C·D·H·I 집중(공간적 병목) 끄기")
    ap.add_argument("--snapshot", type=float, nargs="*", default=[], help="격자 화면 출력 시각(초)")
    ap.add_argument("--ablation", action="store_true", help="OR-Tools만 / Cooperative A*만 조합도 실행")
    args = ap.parse_args()

    os.makedirs(args.out, exist_ok=True)
    print(describe(), "\n")
    jobs = generate_jobs(args.seed, spatial_bottleneck=not args.no_spatial)
    save_jobs_csv(jobs, os.path.join(args.out, "jobs.csv"))
    print(f"주문 {len(jobs)}건 (입고 {sum(j.kind == 'INBOUND' for j in jobs)} / 출고 "
          f"{sum(j.kind == 'OUTBOUND' for j in jobs)}), 마지막 발생 {jobs[-1].release:,.1f}s, "
          f"RFID 실패 주입 {sum(j.rfid_fail for j in jobs)}건\n실행:")

    cases = ["CASE1", "CASE2"] + (["ORTOOLS_ONLY", "COOP_ONLY"] if args.ablation else [])
    results = {}
    for c in cases:
        res, frames = run_case(c, jobs, args.amr, args.snapshot)
        results[c] = res
        for f in frames:
            print(f, "\n")
        save_order_log(res, os.path.join(args.out, f"{c.lower()}_A_orders.csv"))
        save_amr_summary(res, os.path.join(args.out, f"{c.lower()}_B_amr.csv"))
    save_system_summary(list(results.values()), os.path.join(args.out, "C_system_summary.csv"))

    r1, r2 = results["CASE1"], results["CASE2"]
    lines = [
        "# CASE1 vs CASE2 결과", "",
        f"- CASE1: {r1.dispatcher} + {r1.planner}",
        f"- CASE2: {r2.dispatcher} + {r2.planner}",
        f"- 공통: AMR {args.amr}대, 주문 {len(jobs)}건, seed={args.seed}", "",
        compare_table(r1, [r2]), "",
        "개선율 = (CASE1 − CASE2) / CASE1 × 100. 시간당 처리량만 (CASE2 − CASE1) / CASE1. 양수 = CASE2 가 좋음.", "",
        "## C. 시스템 요약", "",
        md_table(["항목"] + [r.name for r in results.values()],
                 [[k] + [f"{r.system_summary()[k]:,.1f}" if isinstance(r.system_summary()[k], float)
                         else r.system_summary()[k] for r in results.values()]
                  for k in r1.system_summary() if k != "CASE"]), "",
    ]
    for r in results.values():
        rows = r.amr_rows()
        lines += [f"## B. AMR별 요약 — {r.name}", "",
                  md_table(list(rows[0].keys()),
                           [[f"{v:,.1f}" if isinstance(v, float) else v for v in x.values()] for x in rows]), ""]
    if args.ablation:
        lines += ["## 레이어별 기여도 (CASE1 대비)", "",
                  compare_table(r1, [results["ORTOOLS_ONLY"], results["COOP_ONLY"], r2]), ""]
    text = "\n".join(lines)
    print("\n" + text)
    with open(os.path.join(args.out, "summary.md"), "w", encoding="utf-8") as f:
        f.write(text)
    print(f"저장: {args.out}/ (jobs.csv, case*_A_orders.csv, case*_B_amr.csv, C_system_summary.csv, summary.md)")


if __name__ == "__main__":
    main()
