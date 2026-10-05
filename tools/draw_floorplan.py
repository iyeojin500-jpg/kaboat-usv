"""창고 배치도(도면) PNG 생성 — AMR 없이 배치만. 좌표는 amr_sim/layout.py 에서 그대로 읽는다.

  python tools/draw_floorplan.py                 # → results/floorplan.png
  python tools/draw_floorplan.py --out plan.png --cell 40
"""
from __future__ import annotations

import argparse
import os
import sys

os.environ.setdefault("SDL_VIDEODRIVER", "dummy")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pygame as pg  # noqa: E402

from amr_sim.layout import (AMR_HOMES, CENTER_CROSS, DOCK_WALLS, HEIGHT, INBOUND_STATION, MAIN_AISLE_Y,  # noqa: E402
                            NARROW_PASSAGE, ONE_WAY, OUTBOUND_STATION, RACKS, RFID_GATE, WIDTH, ZONES)

KOREAN_FONTS = ["malgungothic", "applegothic", "nanumgothic", "notosanscjkkr", "notosanskr",
                "wenquanyizenhei", "wqyzenhei"]
FALLBACK_FILES = ["/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc"]

WHITE = (255, 255, 255)
INK = (35, 38, 45)
SUB = (105, 110, 120)
GRID = (214, 214, 208)
AISLE = (246, 245, 240)
MAIN = (228, 233, 243)
RACK = (88, 96, 112)
FACE = (240, 190, 60)
WALL = (62, 64, 72)
ZONE = {"중앙교차로": (255, 183, 98), "좁은통로": (163, 120, 230), "출고진입부": (238, 130, 165), "RFID": (40, 170, 175)}
IN_C, OUT_C, RFID_C = (40, 150, 75), (125, 60, 170), (20, 140, 150)


def font(size, bold=False):
    for name in KOREAN_FONTS:
        path = pg.font.match_font(name, bold=bold)
        if path:
            return pg.font.Font(path, size)
    for path in FALLBACK_FILES:
        if os.path.exists(path):
            return pg.font.Font(path, size)
    return pg.font.SysFont(None, size, bold=bold)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="results/floorplan.png")
    ap.add_argument("--cell", type=int, default=36)
    args = ap.parse_args()
    c = args.cell
    pg.init()

    left, top, bottom, legend_w = 64, 92, 66, 400
    gw, gh = WIDTH * c, HEIGHT * c
    W, H = left + gw + 40 + legend_w, top + gh + bottom
    scr = pg.Surface((W, H))
    scr.fill(WHITE)
    f_title, f_big, f_mid, f_sm, f_xs = font(26, True), font(int(c * 0.9), True), font(17), font(14), font(12)

    def rect(x, y, w=1, h=1):
        """격자 좌표 (x, y)(왼쪽 아래 기준) → 화면 사각형."""
        return pg.Rect(left + x * c, top + (HEIGHT - y - h) * c, w * c, h * c)

    def center(x, y):
        r = rect(x, y)
        return r.center

    def text(s, f, col, pos, anchor="center"):
        t = f.render(s, True, col)
        r = t.get_rect(**{anchor: pos})
        scr.blit(t, r)
        return r

    # 제목
    text("AMR 자동화 창고 배치도", f_title, INK, (left, 22), "topleft")
    text(f"{WIDTH}m × {HEIGHT}m  ·  1칸 = 1m × 1m  ·  격자 {WIDTH}×{HEIGHT} = {WIDTH * HEIGHT}칸  ·  좌표 (x, y)",
         f_sm, SUB, (left, 58), "topleft")

    # 바닥
    for x in range(WIDTH):
        for y in range(HEIGHT):
            r = rect(x, y)
            if (x, y) in DOCK_WALLS:
                pg.draw.rect(scr, WALL, r)
                for k in range(-c, c, 8):   # 해칭
                    pg.draw.line(scr, (85, 88, 98), (r.left + max(0, k), r.top + max(0, -k)),
                                 (r.left + min(c, c + k), r.top + min(c, c - k)), 1)
                continue
            pg.draw.rect(scr, MAIN if y in MAIN_AISLE_Y else AISLE, r)
    # 공용구역 음영
    for name, cells in ZONES.items():
        for (x, y) in cells:
            s = pg.Surface((c, c), pg.SRCALPHA)
            s.fill((*ZONE[name], 95 if name != "RFID" else 150))
            scr.blit(s, rect(x, y))
    # 격자선
    for x in range(WIDTH + 1):
        pg.draw.line(scr, GRID, (left + x * c, top), (left + x * c, top + gh), 1)
    for y in range(HEIGHT + 1):
        pg.draw.line(scr, GRID, (left, top + y * c), (left + gw, top + y * c), 1)

    # 랙
    for r in RACKS.values():
        x0, x1 = r.x_range
        y0, y1 = r.y_range
        rr = rect(x0, y0, x1 - x0 + 1, y1 - y0 + 1)
        pg.draw.rect(scr, RACK, rr, border_radius=4)
        text(r.name, f_big, WHITE, (rr.centerx, rr.centery - c * 0.35))
        text(r.category, f_xs, (225, 228, 235), (rr.centerx, rr.centery + c * 0.45))
        # 피킹·적치 면 (주 통로 쪽)
        fy = rr.bottom - 3 if r.access_y < r.y0 else rr.top
        pg.draw.rect(scr, FACE, pg.Rect(rr.left + 3, fy, rr.width - 6, 3))

    # 일방통행 화살표
    for (x, y), dirs in ONE_WAY.items():
        dx, dy = next(iter(dirs))
        cx, cy = center(x, y)
        L = c * 0.32
        tip = (cx + dx * L, cy - dy * L)
        tail = (cx - dx * L, cy + dy * L)
        col = (95, 60, 160) if (x, y) in NARROW_PASSAGE else SUB
        pg.draw.line(scr, col, tail, tip, 3)
        px, py = dy, dx
        w = c * 0.16
        back = (tip[0] - dx * c * 0.2, tip[1] + dy * c * 0.2)
        pg.draw.polygon(scr, col, [tip, (back[0] + px * w, back[1] + py * w), (back[0] - px * w, back[1] - py * w)])

    # 스테이션
    for st, label, col in ((INBOUND_STATION, "IN", IN_C), (RFID_GATE, "RFID", RFID_C), (OUTBOUND_STATION, "OUT", OUT_C)):
        r = rect(*st).inflate(-4, -4)
        pg.draw.rect(scr, col, r, border_radius=4)
        text(label, f_xs, WHITE, r.center)
    text("입고장 (15,19)", f_sm, IN_C, (rect(*INBOUND_STATION).right + 6, rect(*INBOUND_STATION).centery), "midleft")
    text("RFID 구역 (15,17)", f_sm, RFID_C, (rect(*RFID_GATE).right + 6, rect(*RFID_GATE).centery), "midleft")

    # 중앙 교차로 표시
    cx, cy = center(*CENTER_CROSS)
    pg.draw.line(scr, (200, 110, 20), (cx - c * 0.3, cy), (cx + c * 0.3, cy), 3)
    pg.draw.line(scr, (200, 110, 20), (cx, cy - c * 0.3), (cx, cy + c * 0.3), 3)
    text("중앙 교차로 (15,10)", f_sm, (170, 90, 10), (cx, rect(*CENTER_CROSS).top - 2), "midbottom")

    # AMR 시작 위치 (3대)
    for i, (x, y) in enumerate(AMR_HOMES[:3]):
        r = rect(x, y).inflate(-8, -8)
        pg.draw.rect(scr, (60, 60, 60), r, 2, border_radius=3)
        text(f"S{i + 1}", f_xs, INK, r.center)

    # 축 눈금
    for x in range(WIDTH):
        text(str(x), f_xs, SUB, (left + x * c + c / 2, top + gh + 6), "midtop")
    for y in range(HEIGHT):
        text(str(y), f_xs, SUB, (left - 8, top + (HEIGHT - 1 - y) * c + c / 2), "midright")
    text("x (m) →", f_sm, SUB, (left + gw, top + gh + 26), "topright")
    text("y (m) ↑", f_sm, SUB, (left - 8, top - 8), "bottomright")

    # 범례
    lx, ly = left + gw + 40, top
    text("범례", f_mid, INK, (lx, ly), "topleft")
    ly += 34

    def swatch(fill, label, desc=None, kind="rect"):
        nonlocal ly
        r = pg.Rect(lx, ly, 26, 20)
        if kind == "zone":
            pg.draw.rect(scr, AISLE, r)
            s = pg.Surface(r.size, pg.SRCALPHA)
            s.fill((*fill, 120))
            scr.blit(s, r)
        else:
            pg.draw.rect(scr, fill, r, border_radius=3)
        pg.draw.rect(scr, GRID, r, 1)
        text(label, f_sm, INK, (lx + 36, ly + 10), "midleft")
        ly += 24
        if desc:
            text(desc, f_xs, SUB, (lx + 36, ly - 2), "topleft")
            ly += 20
        ly += 4

    swatch(RACK, "저장구역 A~J (랙, 통과 불가)", "4m×4m, 랙당 30토트, 노란 선 = 피킹·적치 면")
    swatch(WALL, "벽 (출고 도크)")
    swatch(MAIN, "중앙 주 통로 3m (y=9~11)")
    swatch(AISLE, "일반 통로 (상하좌우 이동, 대각선 없음)")
    swatch(IN_C, "입고장 IN — 모든 주문의 시작점")
    swatch(RFID_C, "RFID 구역 — 공용, 인식 1초")
    swatch(OUT_C, "출고장 OUT — 모든 주문의 종료점")
    text("공용구역 (충돌·병목 측정 위치)", f_sm, INK, (lx, ly + 4), "topleft")
    ly += 30
    swatch(ZONE["중앙교차로"], "중앙 교차로 (x=11~18, y=9~11)", kind="zone")
    swatch(ZONE["좁은통로"], "좁은 통로 (15,2)→(15,1)", "한 번에 1대만, 내려가는 일방통행", kind="zone")
    swatch(ZONE["출고진입부"], "출고 진입부 (14~16,3) + 출고장", kind="zone")
    # 화살표·시작 위치 범례
    pg.draw.line(scr, SUB, (lx + 2, ly + 10), (lx + 22, ly + 10), 3)
    pg.draw.polygon(scr, SUB, [(lx + 26, ly + 10), (lx + 18, ly + 5), (lx + 18, ly + 15)])
    text("일방통행 (출구 x=12·18 은 올라가는 방향)", f_sm, INK, (lx + 36, ly + 10), "midleft")
    ly += 32
    pg.draw.rect(scr, (60, 60, 60), pg.Rect(lx + 3, ly + 1, 20, 18), 2, border_radius=3)
    text("S1~S3  AMR 시작 위치", f_sm, INK, (lx + 36, ly + 10), "midleft")
    ly += 40
    text("주문 1건 흐름", f_mid, INK, (lx, ly), "topleft")
    ly += 30
    for step in ["① 입고장 적재", "② RFID 구역 인식", "③ 저장구역 A~J 적치 + 피킹",
                 "④ 중앙 교차로 통과", "⑤ 좁은 통로 (1대씩)", "⑥ 출고장 하역", "⑦ 출구 x=12 / 18 로 빠져나감"]:
        text(step, f_sm, INK, (lx + 4, ly), "topleft")
        ly += 22

    os.makedirs(os.path.dirname(args.out) or ".", exist_ok=True)
    pg.image.save(scr, args.out)
    print(f"저장: {args.out} ({W}x{H})")


if __name__ == "__main__":
    main()
