"""CASE1 — 기존 인력 방식 (작업자 4명 + 카트 + 바코드 + WMS).

- 배정: 작업을 발생순(FIFO)으로, 가장 먼저 비는 작업자에게 (동시에 비면 번호 작은 작업자)
- 사람끼리는 통로에서 서로 비켜 지나간다고 보고 충돌/병목 대기는 두지 않는다.
- 작업자는 작업을 마친 위치에서 다음 작업을 시작한다.

입고: 입고장까지 도보 → 바코드 스캔 → WMS 등록 → 카트 상차 → 랙까지 도보 → 카트 하차 → 적치 → WMS 갱신
출고: 랙까지 도보 → 피킹 → 카트 상차 → 출고장까지 도보 → 카트 하차 → 바코드 확인 → WMS 처리
"""
from __future__ import annotations

import random

from . import params as P
from .jobs import Job
from .layout import INBOUND_STATION, OUTBOUND_STATION
from .metrics import CaseResult, JobRecord, max_queue_from_records
from .pathfinding import distance


class Worker:
    def __init__(self, idx: int, pos):
        self.idx = idx
        self.name = f"작업자{idx + 1}"
        self.pos = pos
        self.free_at = 0.0
        self.distance = 0
        self.move_time = 0.0
        self.jobs = 0
        self.busy = 0.0


def run_case1(jobs: list[Job], n_workers: int = P.CASE1_WORKERS, seed: int = P.SEED) -> CaseResult:
    rng = random.Random(seed)
    starts = [INBOUND_STATION, OUTBOUND_STATION]
    workers = [Worker(i, starts[i % 2]) for i in range(n_workers)]
    records: list[JobRecord] = []

    for job in sorted(jobs, key=lambda j: (j.release, j.job_id)):
        w = min(workers, key=lambda w: (max(w.free_at, job.release), w.idx))
        t0 = max(w.free_at, job.release)
        rec = JobRecord(job.job_id, job.kind, job.rack, job.release, assign=t0, start=t0, resource=w.name)

        if job.kind == "INBOUND":
            d1 = distance(w.pos, INBOUND_STATION)
            d2 = distance(INBOUND_STATION, job.slot_cell)
            ident = P.jitter(rng, P.BARCODE_SCAN) + P.WMS_PROCESS + P.WMS_PROCESS   # 스캔 + 등록 + 갱신
            handle = P.jitter(rng, P.CART_LOAD) + P.jitter(rng, P.CART_UNLOAD) + P.jitter(rng, P.HUMAN_PLACE)
            end_pos = job.slot_cell
        else:
            d1 = distance(w.pos, job.slot_cell)
            d2 = distance(job.slot_cell, OUTBOUND_STATION)
            handle = P.jitter(rng, P.HUMAN_PICK) + P.jitter(rng, P.CART_LOAD) + P.jitter(rng, P.CART_UNLOAD)
            ident = P.jitter(rng, P.BARCODE_SCAN) + P.WMS_PROCESS
            end_pos = OUTBOUND_STATION

        move = (d1 + d2) / P.HUMAN_SPEED
        rec.move_time, rec.handling_time, rec.id_time = move, handle, ident
        rec.distance = d1 + d2
        rec.complete = t0 + move + handle + ident
        records.append(rec)

        w.pos, w.free_at = end_pos, rec.complete
        w.distance += d1 + d2
        w.move_time += move
        w.jobs += 1
        w.busy += rec.complete - t0

    result = CaseResult(
        name="CASE1",
        records=records,
        total_distance=sum(w.distance for w in workers),
        worker_move_time=sum(w.move_time for w in workers),
        max_queue=max_queue_from_records(records),
    )
    ms = result.makespan
    result.resources = [{"자원": w.name, "처리건수": w.jobs, "이동거리(m)": w.distance,
                         "가동률(%)": 100 * w.busy / ms, "시간당 처리(건/h)": w.jobs / (ms / 3600)}
                        for w in workers]
    return result
