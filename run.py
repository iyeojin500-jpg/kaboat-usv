"""CASE1 · CASE2 시뮬레이션 실행 (헤드리스).

사용 예:
  python3 run.py                         # 기본 실행 → results/ 에 CSV·결과표 저장
  python3 run.py --snapshot 4300 12500   # CASE2 해당 시각의 격자 화면 출력
  python3 run.py --amr 4                 # 가정값 민감도 확인 (AMR 대수 변경)
"""
from __future__ import annotations

import argparse
import csv
import os

from amr_sim import params as P
from amr_sim.case1 import run_case1
from amr_sim.case2 import run_case2
from amr_sim.jobs import generate_jobs, save_jobs_csv
from amr_sim.layout import describe
from amr_sim.metrics import CaseResult, save_records_csv

UNITS = {
    "1,000건 완료시간(Makespan)": "s", "평균 Lead Time": "s", "평균 대기시간": "s", "총 이동거리": "m",
    "최대 대기 작업 수(Queue)": "건", "시간당 처리량": "건/h", "AMR 충돌/대기 횟수": "회",
    "AMR 병목 대기시간": "s", "작업자 총 이동시간": "s",
}
HIGHER_IS_BETTER = {"시간당 처리량"}


def fmt(v, unit: str) -> str:
    if v is None:
        return "-"
    if unit == "s":
        h, rem = divmod(v, 3600)
        return f"{v:,.1f}s ({int(h)}h{int(rem // 60):02d}m)" if v >= 3600 else f"{v:,.1f}s"
    if unit in ("건", "회", "m"):
        return f"{v:,.0f}{unit}"
    return f"{v:,.1f}{unit}"


def improvement(k: str, c1, c2) -> str:
    if c1 in (None, 0) or c2 is None:
        return "-"
    if k in HIGHER_IS_BETTER:
        return f"{(c2 - c1) / c1 * 100:+.1f}%"
    return f"{(c1 - c2) / c1 * 100:+.1f}%"


def result_table(r1: CaseResult, r2: CaseResult) -> list[list[str]]:
    k1, k2 = r1.kpis(), r2.kpis()
    rows = []
    for k in k1:
        c1 = None if k.startswith("AMR") else k1[k]
        rows.append([k, fmt(c1, UNITS[k]), fmt(k2[k], UNITS[k]), improvement(k, c1, k2[k])])
    return rows


def md_table(header, rows) -> str:
    out = ["| " + " | ".join(header) + " |", "|" + "|".join("---" for _ in header) + "|"]
    out += ["| " + " | ".join(str(c) for c in r) + " |" for r in rows]
    return "\n".join(out)


def resource_table(res: CaseResult) -> str:
    header = list(res.resources[0].keys())
    rows = [[f"{v:,.1f}" if isinstance(v, float) else v for v in r.values()] for r in res.resources]
    return md_table(header, rows)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--seed", type=int, default=P.SEED)
    ap.add_argument("--out", default="results")
    ap.add_argument("--amr", type=int, default=P.AMR_COUNT, help="CASE2 AMR 대수")
    ap.add_argument("--workers", type=int, default=P.CASE1_WORKERS, help="CASE1 작업자 수")
    ap.add_argument("--no-spatial", action="store_true", help="Peak 구간 C·D·H·I 집중(공간적 병목) 끄기")
    ap.add_argument("--snapshot", type=float, nargs="*", default=[], help="CASE2 화면 출력 시각(초)")
    args = ap.parse_args()

    os.makedirs(args.out, exist_ok=True)
    print(describe(), "\n")

    jobs = generate_jobs(args.seed, spatial_bottleneck=not args.no_spatial)
    save_jobs_csv(jobs, os.path.join(args.out, "jobs.csv"))
    print(f"작업 {len(jobs)}건 생성 (입고 {sum(j.kind == 'INBOUND' for j in jobs)} / "
          f"출고 {sum(j.kind == 'OUTBOUND' for j in jobs)}), 마지막 발생 {jobs[-1].release:,.1f}s\n")

    r1 = run_case1(jobs, n_workers=args.workers, seed=args.seed)
    save_records_csv(r1.records, os.path.join(args.out, "case1_jobs.csv"))
    r2, frames = run_case2(jobs, seed=args.seed, snapshot_times=args.snapshot, n_amr=args.amr)
    save_records_csv(r2.records, os.path.join(args.out, "case2_jobs.csv"))

    for f in frames:
        print(f, "\n")

    rows = result_table(r1, r2)
    header = ["KPI", f"CASE1 (작업자 {args.workers})", f"CASE2 (AMR {args.amr})", "개선율"]
    report = [
        "# 결과표", "", md_table(header, rows), "",
        "개선율(%) = (CASE1 − CASE2) / CASE1 × 100 (시간당 처리량은 높을수록 좋으므로 (CASE2 − CASE1) / CASE1).",
        "양수 = CASE2 가 더 좋음, 음수 = CASE2 가 더 나쁨.", "",
        "## CASE1 작업자별", "", resource_table(r1), "",
        "## CASE2 AMR별 (처리량·병목대기는 시뮬레이션 결과로 산출)", "", resource_table(r2), "",
    ]
    text = "\n".join(report)
    print(text)
    with open(os.path.join(args.out, "summary.md"), "w", encoding="utf-8") as f:
        f.write(text)
    with open(os.path.join(args.out, "summary.csv"), "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["KPI", "CASE1", "CASE2", "개선율"])
        k1, k2 = r1.kpis(), r2.kpis()
        for k in k1:
            c1 = None if k.startswith("AMR") else k1[k]
            w.writerow([k, "" if c1 is None else round(c1, 2), "" if k2[k] is None else round(k2[k], 2),
                        improvement(k, c1, k2[k])])
    print(f"CSV 저장: {args.out}/jobs.csv, case1_jobs.csv, case2_jobs.csv, summary.csv, summary.md")


if __name__ == "__main__":
    main()
