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
  y=3~4   : 하단 통로 2m
  y=0~2   : 출고 도크. 벽(y=1~2) 사이로
              - 좁은 통로 x=15, y=2→1 (내려가는 일방통행, 한 번에 1대만) → 출고 STATION (15,0)
              - 출구 x=12·x=18, y=0→2 (올라가는 일방통행). 출고 후 y=0 을 따라 좌/우로 빠져나감

공용구역 (충돌·병목 측정 위치)
------------------------------
  RFID 구역   : (15,17) — 입고 STATION (15,19) 바로 아래, 모든 주문이 통과·인식(1초)
  중앙 교차로 : 주 통로 x=11~18, y=9~11 (대표점 (15,10))
  좁은 통로   : (15,2), (15,1) — 구역 전체를 한 번에 1대만 점유
  출고 진입부 : 좁은 통로 입구 (14~16, 3) + 출고 STATION (15,0)

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
OUTBOUND_STATION = (15, 0)
CENTER_CROSS = (15, 10)
RFID_GATE = (15, 17)

# 출고 도크: 벽 + 좁은 통로(일방통행·1대) + 출구(일방통행)
DOCK_WALLS = frozenset([(x, y) for x in range(WIDTH) for y in (1, 2) if x not in (12, 15, 18)]
                       + [(x, 0) for x in range(WIDTH) if x < 12 or x > 18])
NARROW_PASSAGE = frozenset([(15, 2), (15, 1)])
NARROW_CAPACITY = 1
# 칸에 "들어갈 때" 허용되는 이동 방향 (없는 칸은 모든 방향 허용)
ONE_WAY: dict[tuple[int, int], frozenset] = {
    (15, 2): frozenset({(0, -1)}), (15, 1): frozenset({(0, -1)}), (15, 0): frozenset({(0, -1)}),
    **{(x, 0): frozenset({(-1, 0)}) for x in (12, 13, 14)},
    **{(x, 0): frozenset({(1, 0)}) for x in (16, 17, 18)},
    **{(x, y): frozenset({(0, 1)}) for x in (12, 18) for y in (1, 2)},
}

# 공용구역: 이름 → 칸 집합 (병목 판정에 사용)
ZONES: dict[str, frozenset] = {
    "RFID": frozenset([RFID_GATE]),
    "중앙교차로": frozenset((x, y) for x in range(11, 19) for y in (9, 10, 11)),
    "좁은통로": NARROW_PASSAGE,
    "출고진입부": frozenset([(14, 3), (15, 3), (16, 3), OUTBOUND_STATION]),
}
CELL_ZONE = {c: z for z, cells in ZONES.items() for c in cells}

# 세로 통로(벽 통로 포함) x 좌표. 이 x 와 가로 통로(주 통로 y=9~11, 상단 y=16, 하단 y=4)가
# 만나는 셀을 교차로로 보고 AMR 교차로 통과 속도를 적용한다.
VERTICAL_AISLE_X = (0, 5, 6, 11, 12, 17, 18, 23, 24, 29)
INTERSECTIONS = frozenset((x, y) for x in VERTICAL_AISLE_X for y in (4, 9, 10, 11, 16))

BOTTLENECK_ZONE = frozenset(CELL_ZONE)

# AMR 시작(주차) 위치: 3대 서로 다른 위치
AMR_HOMES = [(0, 19), (29, 19), (0, 3), (29, 3), (2, 19), (27, 19)]

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

    one_way: dict = field(default_factory=dict)

    def can_move(self, c: tuple[int, int], n: tuple[int, int]) -> bool:
        """c → n 이동 가능 여부 (장애물·일방통행 반영, 대각선 없음)."""
        if not self.passable(n):
            return False
        allowed = self.one_way.get(n)
        return allowed is None or (n[0] - c[0], n[1] - c[1]) in allowed

    def neighbors(self, c: tuple[int, int]):
        x, y = c
        for n in ((x + 1, y), (x - 1, y), (x, y + 1), (x, y - 1)):
            if self.can_move(c, n):
                yield n

    def free_cells(self) -> list[tuple[int, int]]:
        return [(x, y) for x in range(self.width) for y in range(self.height)
                if (x, y) not in self.blocked]


def build_grid() -> Grid:
    g = Grid(one_way=dict(ONE_WAY))
    for r in RACKS.values():
        g.blocked.update(r.cells)
    g.blocked.update(DOCK_WALLS)
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
    for label, p in (("입고", INBOUND_STATION), ("출고", OUTBOUND_STATION), ("교차로", CENTER_CROSS),
                     ("RFID", RFID_GATE)):
        if not grid.passable(p):
            errors.append(f"{label} 지점 {p} 이 통행 불가")
    # 모든 통로 셀이 서로 오갈 수 있는지 (일방통행 포함, 정방향·역방향 BFS)
    free = grid.free_cells()
    for direction in ("정방향", "역방향"):
        start = INBOUND_STATION
        visited = {start}
        q = deque([start])
        while q:
            c = q.popleft()
            x, y = c
            for n in ((x + 1, y), (x - 1, y), (x, y + 1), (x, y - 1)):
                ok = grid.can_move(c, n) if direction == "정방향" else (grid.passable(n) and grid.can_move(n, c))
                if ok and n not in visited:
                    visited.add(n)
                    q.append(n)
        if len(visited) != len(free):
            errors.append(f"{direction}으로 도달 불가 통로 셀 {len(free) - len(visited)}개")
    return errors


def render_ascii(grid: Grid) -> str:
    """격자 지도를 문자열로 (위쪽 = y 19)."""
    marks = {INBOUND_STATION: "I", OUTBOUND_STATION: "O", CENTER_CROSS: "+", RFID_GATE: "R"}
    arrows = {(0, -1): "v", (0, 1): "^", (-1, 0): "<", (1, 0): ">"}
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
            elif c in DOCK_WALLS:
                row.append("#")
            elif c in ONE_WAY:
                row.append(arrows[next(iter(ONE_WAY[c]))])
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
           "범례: A~J 랙(장애물)  # 벽  : 랙 접근 셀  = 중앙 주 통로  . 일반 통로",
           "      I 입고 STATION  R RFID 구역  O 출고 STATION  + 중앙 교차로",
           "      v ^ < > 일방통행 (x=15 v v = 좁은 통로, 1대씩)", "",
           f"격자 {grid.width}x{grid.height} = {grid.width * grid.height} grid, "
           f"랙 셀 {len(grid.blocked)}, 통로 셀 {len(grid.free_cells())}", "",
           "랙 | 분류           | x 범위 | y 범위 | 접근 y | 대표 접근점 | 용량",
           "---|----------------|--------|--------|--------|-------------|-----"]
    for r in RACKS.values():
        out.append(f" {r.name} | {r.category:<14} | {r.x_range[0]:>2}~{r.x_range[1]:<2}  | "
                   f"{r.y_range[0]:>2}~{r.y_range[1]:<2}  |   {r.access_y:>2}   | "
                   f"{str(r.representative):<11} | {RACK_CAPACITY}")
    out += ["",
            f"입고 STATION {INBOUND_STATION}, RFID {RFID_GATE}, 출고 STATION {OUTBOUND_STATION}, "
            f"중앙 교차로 {CENTER_CROSS}, 좁은 통로 {sorted(NARROW_PASSAGE)}",
            "공용구역: " + ", ".join(f"{z} {len(c)}칸" for z, c in ZONES.items()),
            f"랙 용량 {RACK_CAPACITY} x 10 = {RACK_CAPACITY * 10} 토트, 슬롯 예: "
            f"{RACKS['A'].slot_id(2, 3)} → 접근 셀 {RACKS['A'].slot_access(2)}"]
    errors = validate(grid)
    out.append("검증: " + ("OK (겹침 없음, 일방통행 포함 모든 통로 상호 도달 가능)" if not errors else "; ".join(errors)))
    return "\n".join(out)


if __name__ == "__main__":
    print(describe())
