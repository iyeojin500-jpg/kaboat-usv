"""창고 격자·랙·통로 좌표 정의 (구현 순서 1단계).

좌표계
------
- 1 grid = 1m x 1m, 가로 30(x: 0~29) x 세로 20(y: 0~19) = 600 grid
- (x, y) 표기. x 는 왼쪽→오른쪽, y 는 아래(출고측)→위(입고측)
- 랙 셀 = 장애물(통과 불가), 나머지 셀 = 통로(이동 가능)

배치 (x 방향)
-------------
  x=0        : 좌측 벽 통로 1m
  랙 4m + 통로 2m 반복 5회 → 랙 x 시작 = 1, 7, 13, 19, 25
  x=29       : 우측 벽 통로 1m
  → 랙 20m + 통로 10m = 30m

배치 (y 방향)
-------------
  y=16~19 : 상단 통로 4m (입고 STATION (15,19))
  y=12~15 : 상단 랙 A~E (깊이 4m)
  y=9~11  : 중앙 주 통로 3m (중앙 교차로 (15,10))
  y=5~8   : 하단 랙 F~J (깊이 4m)
  y=0~4   : 하단 통로 5m (출고 STATION (15,1))
  → 20m 가 짝수라 주 통로(3m) 를 y=10 중심에 두면 상/하단 통로가 4m/5m 로 1m 비대칭.

랙 접근면
---------
랙의 앞면(피킹/적치면)은 중앙 주 통로를 향한다.
  - 상단 랙 A~E : 접근 셀 y = 11
  - 하단 랙 F~J : 접근 셀 y = 9
따라서 모든 피킹/적치는 주 통로에서 일어나고, 입고(위)·출고(아래) STATION 과
주 통로를 잇는 세로 통로(x=5~6, 11~12, 17~18, 23~24)와 주 통로 중앙부가 병목이 된다.

랙 슬롯
-------
한 층 6토트 x 5단 = 30토트. 슬롯 ID = "{랙}-{열:02d}-{단:02d}" (예: A-02-03).
토트 폭 0.6m x 6열 = 3.6m ≤ 랙 폭 4m. 열 c(1~6) 의 접근 셀 x = 랙 x0 + [0,0,1,2,2,3][c-1].
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field

# ---------------------------------------------------------------- 기본 치수
WIDTH = 30   # x: 0..29
HEIGHT = 20  # y: 0..19

RACK_W = 4   # 랙 가로 (x)
RACK_D = 4   # 랙 깊이 (y)
RACK_X0 = [1, 7, 13, 19, 25]          # 랙 5개의 x 시작 좌표 (통로 2m 간격)
TOP_RACK_Y0 = 12                      # 상단 랙 A~E: y=12..15
BOTTOM_RACK_Y0 = 5                    # 하단 랙 F~J: y=5..8
MAIN_AISLE_Y = (9, 10, 11)            # 중앙 주 통로 3m

SLOT_COLS = 6
SLOT_LEVELS = 5
RACK_CAPACITY = SLOT_COLS * SLOT_LEVELS   # 30
_COL_OFFSET = [0, 0, 1, 2, 2, 3]          # 열 → 랙 앞면 x 오프셋

# ---------------------------------------------------------------- 주요 지점
INBOUND_STATION = (15, 19)
OUTBOUND_STATION = (15, 1)
CENTER_CROSS = (15, 10)

# 세로 통로(벽 통로 포함) x 좌표. 이 x 와 가로 통로(주 통로 y=9~11, 상단 y=16, 하단 y=4)가
# 만나는 셀을 교차로로 보고 AMR 교차로 통과 속도를 적용한다.
VERTICAL_AISLE_X = (0, 5, 6, 11, 12, 17, 18, 23, 24, 29)
INTERSECTIONS = frozenset((x, y) for x in VERTICAL_AISLE_X for y in (4, 9, 10, 11, 16))

# AMR 대기(주차) 위치: 동선과 겹치지 않는 창고 모서리
AMR_HOMES = [(0, 19), (29, 19), (0, 0), (29, 0), (2, 19), (27, 19)]

RACK_CATEGORY = {
    "A": "체결부품", "B": "베어링·회전부품", "C": "전선·케이블", "D": "커넥터·단자",
    "E": "센서류", "F": "스위치·버튼", "G": "제어·전자부품", "H": "전원부품",
    "I": "공압·배관부품", "J": "유지보수 소모품",
}


@dataclass(frozen=True)
class Rack:
    name: str
    x0: int
    y0: int
    access_y: int                     # 앞면(주 통로 쪽) 접근 셀의 y
    category: str

    @property
    def cells(self) -> list[tuple[int, int]]:
        return [(x, y) for x in range(self.x0, self.x0 + RACK_W)
                for y in range(self.y0, self.y0 + RACK_D)]

    @property
    def x_range(self) -> tuple[int, int]:
        return self.x0, self.x0 + RACK_W - 1

    @property
    def y_range(self) -> tuple[int, int]:
        return self.y0, self.y0 + RACK_D - 1

    @property
    def access_cells(self) -> list[tuple[int, int]]:
        return [(self.x0 + dx, self.access_y) for dx in range(RACK_W)]

    @property
    def representative(self) -> tuple[int, int]:
        """랙 대표 접근점 (앞면 중앙)."""
        return (self.x0 + RACK_W // 2 - 1, self.access_y)

    def slot_id(self, col: int, level: int) -> str:
        return f"{self.name}-{col:02d}-{level:02d}"

    def slot_access(self, col: int) -> tuple[int, int]:
        """슬롯 열(1~6) 을 피킹/적치할 때 AMR·작업자가 서는 셀."""
        return (self.x0 + _COL_OFFSET[col - 1], self.access_y)

    def slots(self) -> list[str]:
        return [self.slot_id(c, l) for l in range(1, SLOT_LEVELS + 1)
                for c in range(1, SLOT_COLS + 1)]


def _build_racks() -> dict[str, Rack]:
    racks: dict[str, Rack] = {}
    for i, name in enumerate("ABCDE"):
        racks[name] = Rack(name, RACK_X0[i], TOP_RACK_Y0, TOP_RACK_Y0 - 1, RACK_CATEGORY[name])
    for i, name in enumerate("FGHIJ"):
        racks[name] = Rack(name, RACK_X0[i], BOTTOM_RACK_Y0, BOTTOM_RACK_Y0 + RACK_D, RACK_CATEGORY[name])
    return racks


RACKS: dict[str, Rack] = _build_racks()


@dataclass
class Grid:
    width: int = WIDTH
    height: int = HEIGHT
    blocked: set[tuple[int, int]] = field(default_factory=set)

    def in_bounds(self, c: tuple[int, int]) -> bool:
        return 0 <= c[0] < self.width and 0 <= c[1] < self.height

    def passable(self, c: tuple[int, int]) -> bool:
        return self.in_bounds(c) and c not in self.blocked

    def neighbors(self, c: tuple[int, int]):
        x, y = c
        for n in ((x + 1, y), (x - 1, y), (x, y + 1), (x, y - 1)):
            if self.passable(n):
                yield n

    def free_cells(self) -> list[tuple[int, int]]:
        return [(x, y) for x in range(self.width) for y in range(self.height)
                if (x, y) not in self.blocked]


def build_grid() -> Grid:
    g = Grid()
    for r in RACKS.values():
        g.blocked.update(r.cells)
    return g


def validate(grid: Grid) -> list[str]:
    """배치 검증: 랙 겹침/범위, 주요 지점 통행 가능, 전체 통로 연결성."""
    errors: list[str] = []
    seen: dict[tuple[int, int], str] = {}
    for r in RACKS.values():
        for c in r.cells:
            if not grid.in_bounds(c):
                errors.append(f"랙 {r.name} 셀 {c} 이 창고 범위를 벗어남")
            if c in seen:
                errors.append(f"랙 {r.name} 과 {seen[c]} 이 {c} 에서 겹침")
            seen[c] = r.name
        for col in range(1, SLOT_COLS + 1):
            if not grid.passable(r.slot_access(col)):
                errors.append(f"랙 {r.name} 열 {col} 접근 셀이 막혀 있음")
    for label, p in (("입고", INBOUND_STATION), ("출고", OUTBOUND_STATION), ("교차로", CENTER_CROSS)):
        if not grid.passable(p):
            errors.append(f"{label} 지점 {p} 이 통행 불가")
    # 모든 통로 셀이 하나로 연결되어 있는지 (BFS)
    start = INBOUND_STATION
    visited = {start}
    q = deque([start])
    while q:
        for n in grid.neighbors(q.popleft()):
            if n not in visited:
                visited.add(n)
                q.append(n)
    free = grid.free_cells()
    if len(visited) != len(free):
        errors.append(f"고립된 통로 셀 {len(free) - len(visited)}개")
    return errors


def render_ascii(grid: Grid) -> str:
    """격자 지도를 문자열로 (위쪽 = y 19)."""
    marks = {INBOUND_STATION: "I", OUTBOUND_STATION: "O", CENTER_CROSS: "+"}
    cell_rack = {c: r.name for r in RACKS.values() for c in r.cells}
    access = {c for r in RACKS.values() for c in r.access_cells}
    lines = ["     " + "".join(f"{x // 10 if x >= 10 else ' '}" for x in range(grid.width)),
             "     " + "".join(str(x % 10) for x in range(grid.width))]
    for y in range(grid.height - 1, -1, -1):
        row = []
        for x in range(grid.width):
            c = (x, y)
            if c in marks:
                row.append(marks[c])
            elif c in cell_rack:
                row.append(cell_rack[c])
            elif c in access:
                row.append(":")
            elif y in MAIN_AISLE_Y:
                row.append("=")
            else:
                row.append(".")
        lines.append(f"y={y:2d} " + "".join(row))
    return "\n".join(lines)


def describe() -> str:
    grid = build_grid()
    out = [render_ascii(grid), "",
           "범례: A~J 랙(장애물)  : 랙 접근 셀  = 중앙 주 통로  . 일반 통로",
           "      I 입고 STATION  O 출고 STATION  + 중앙 교차로", "",
           f"격자 {grid.width}x{grid.height} = {grid.width * grid.height} grid, "
           f"랙 셀 {len(grid.blocked)}, 통로 셀 {len(grid.free_cells())}", "",
           "랙 | 분류           | x 범위 | y 범위 | 접근 y | 대표 접근점 | 용량",
           "---|----------------|--------|--------|--------|-------------|-----"]
    for r in RACKS.values():
        out.append(f" {r.name} | {r.category:<14} | {r.x_range[0]:>2}~{r.x_range[1]:<2}  | "
                   f"{r.y_range[0]:>2}~{r.y_range[1]:<2}  |   {r.access_y:>2}   | "
                   f"{str(r.representative):<11} | {RACK_CAPACITY}")
    out += ["",
            f"입고 STATION {INBOUND_STATION}, 출고 STATION {OUTBOUND_STATION}, 중앙 교차로 {CENTER_CROSS}",
            f"랙 용량 {RACK_CAPACITY} x 10 = {RACK_CAPACITY * 10} 토트, 슬롯 예: "
            f"{RACKS['A'].slot_id(2, 3)} → 접근 셀 {RACKS['A'].slot_access(2)}"]
    errors = validate(grid)
    out.append("검증: " + ("OK (겹침 없음, 모든 통로 연결됨)" if not errors else "; ".join(errors)))
    return "\n".join(out)


if __name__ == "__main__":
    print(describe())
