"""JOB 별 기록(CSV) 과 KPI 계산."""
from __future__ import annotations

import csv
from dataclasses import dataclass, field


@dataclass
class JobRecord:
    job_id: int
    kind: str
    rack: str
    release: float
    assign: float = 0.0
    start: float = 0.0
    complete: float = 0.0
    move_time: float = 0.0
    handling_time: float = 0.0     # 피킹/적치 (+카트 상하차, 로봇팔 적재/하역)
    id_time: float = 0.0           # RFID / 바코드 / WMS
    block_time: float = 0.0        # 병목 대기 (다른 AMR 때문에 정지 + 재출발 지연)
    block_events: int = 0
    station_wait: float = 0.0      # 입고장에서 RFID 태그 부착 완료 대기 (CASE2)
    distance: int = 0
    resource: str = ""

    @property
    def wait(self) -> float:
        return self.assign - self.release

    @property
    def lead_time(self) -> float:
        return self.complete - self.release


CSV_HEADER = ["JOB_ID", "종류", "랙", "발생시각", "배정시각", "작업시작시각", "작업완료시각",
              "대기시간", "이동시간", "피킹/적치시간", "RFID/바코드시간", "병목대기시간",
              "병목정지횟수", "스테이션대기시간", "리드타임", "이동거리(m)", "담당"]


def save_records_csv(records: list[JobRecord], path: str) -> None:
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(CSV_HEADER)
        for r in sorted(records, key=lambda r: r.job_id):
            w.writerow([r.job_id, r.kind, r.rack, f"{r.release:.2f}", f"{r.assign:.2f}", f"{r.start:.2f}",
                        f"{r.complete:.2f}", f"{r.wait:.2f}", f"{r.move_time:.2f}", f"{r.handling_time:.2f}",
                        f"{r.id_time:.2f}", f"{r.block_time:.2f}", r.block_events, f"{r.station_wait:.2f}",
                        f"{r.lead_time:.2f}", r.distance, r.resource])


def max_queue_from_records(records: list[JobRecord]) -> int:
    """발생했지만 아직 배정되지 않은 작업 수의 최댓값."""
    events = [(r.release, 1) for r in records] + [(r.assign, -1) for r in records]
    events.sort(key=lambda e: (e[0], e[1]))  # 같은 시각이면 배정(-1) 먼저
    q = best = 0
    for _, d in events:
        q += d
        best = max(best, q)
    return best


@dataclass
class CaseResult:
    name: str
    records: list[JobRecord]
    total_distance: float
    worker_move_time: float
    max_queue: int
    amr_block_events: int | None = None
    amr_block_time: float | None = None
    resources: list[dict] = field(default_factory=list)   # 자원별 통계

    @property
    def makespan(self) -> float:
        return max(r.complete for r in self.records)

    @property
    def avg_lead(self) -> float:
        return sum(r.lead_time for r in self.records) / len(self.records)

    @property
    def avg_wait(self) -> float:
        return sum(r.wait for r in self.records) / len(self.records)

    @property
    def throughput(self) -> float:
        return len(self.records) / (self.makespan / 3600)

    def kpis(self) -> dict[str, float | None]:
        return {
            "1,000건 완료시간(Makespan)": self.makespan,
            "평균 Lead Time": self.avg_lead,
            "평균 대기시간": self.avg_wait,
            "총 이동거리": self.total_distance,
            "최대 대기 작업 수(Queue)": self.max_queue,
            "시간당 처리량": self.throughput,
            "AMR 충돌/대기 횟수": self.amr_block_events,
            "AMR 병목 대기시간": self.amr_block_time,
            "작업자 총 이동시간": self.worker_move_time,
        }
