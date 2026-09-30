"""시각화 모드 (pygame) — CASE1 과 CASE2 를 같은 시계로 나란히 재생.

  python3 visualize.py                     # 두 CASE 나란히, 시작부터
  python3 visualize.py --start 4000        # 4000초(피크1)까지 빠르게 계산한 뒤 재생
  python3 visualize.py --case CASE2        # 한 CASE 만
  python3 visualize.py --shot out.png --start 4300   # 창 없이 그 시각 화면을 PNG 로 저장

조작: SPACE 일시정지 · ↑/↓ 배속 x2 / ÷2 · → 1초 진행(일시정지 중) · S 스크린샷 · ESC 종료

표시: 랙(회색 블록), IN/OUT 스테이션, 주황 음영 = 병목구간, 원 = AMR (번호),
      원 안 사각형 = 토트 적재, 빨간 테두리 = 다른 AMR 때문에 대기 중, 선 = 앞으로 갈 경로, X = 목적지
"""
from __future__ import annotations

import argparse
import os

from amr_sim import params as P
from amr_sim.cases import make_sim
from amr_sim.jobs import generate_jobs
from amr_sim.layout import BOTTLENECK_ZONE, HEIGHT, INBOUND_STATION, MAIN_AISLE_Y, OUTBOUND_STATION, RACKS, WIDTH

AMR_COLORS = [(220, 60, 60), (40, 110, 220), (30, 160, 80), (200, 130, 0), (150, 70, 200), (0, 150, 160)]
BG = (245, 245, 242)
AISLE = (232, 232, 228)
MAIN = (220, 222, 230)
RACK = (110, 115, 125)
ZONE = (255, 190, 110)
TEXT = (30, 30, 35)
SUB = (100, 100, 110)
LABEL = {"CASE1": "CASE1  Sequential dispatch + Independent A*",
         "CASE2": "CASE2  OR-Tools VRP + Cooperative A* / Reservation",
         "ORTOOLS_ONLY": "OR-Tools VRP + Independent A*",
         "COOP_ONLY": "Sequential + Cooperative A*"}


class Pane:
    def __init__(self, sim, x0, y0, cell):
        self.sim, self.x0, self.y0, self.c = sim, x0, y0, cell

    def px(self, x, y):
        """격자 (x, y) 중심 → 화면 좌표 (y 는 위가 19)."""
        return self.x0 + x * self.c + self.c / 2, self.y0 + (HEIGHT - 1 - y) * self.c + self.c / 2

    def amr_pos(self, a):
        x, y = a.cell
        if a.next_cell is not None and a.restart_left <= 0:
            f = 1 - a.move_left / max(1, a.move_dur)
            x += (a.next_cell[0] - x) * f
            y += (a.next_cell[1] - y) * f
        return self.px(x, y)

    def draw(self, pg, scr, fonts):
        sim, c = self.sim, self.c
        big, mid, small = fonts
        for x in range(WIDTH):
            for y in range(HEIGHT):
                col = MAIN if y in MAIN_AISLE_Y else AISLE
                rect = pg.Rect(self.x0 + x * c, self.y0 + (HEIGHT - 1 - y) * c, c, c)
                pg.draw.rect(scr, col, rect)
                if (x, y) in BOTTLENECK_ZONE:
                    s = pg.Surface((c, c), pg.SRCALPHA)
                    s.fill((*ZONE, 70))
                    scr.blit(s, rect)
                pg.draw.rect(scr, (210, 210, 205), rect, 1)
        for r in RACKS.values():
            x0, x1 = r.x_range
            y0, y1 = r.y_range
            rect = pg.Rect(self.x0 + x0 * c, self.y0 + (HEIGHT - 1 - y1) * c, (x1 - x0 + 1) * c, (y1 - y0 + 1) * c)
            pg.draw.rect(scr, RACK, rect, border_radius=3)
            t = big.render(r.name, True, (255, 255, 255))
            scr.blit(t, t.get_rect(center=rect.center))
        for st, name, col in ((INBOUND_STATION, "IN", (40, 150, 70)), (OUTBOUND_STATION, "OUT", (130, 60, 170))):
            cx, cy = self.px(*st)
            rect = pg.Rect(0, 0, c * 1.6, c * 0.9)
            rect.center = (cx, cy)
            pg.draw.rect(scr, col, rect, border_radius=4)
            t = small.render(name, True, (255, 255, 255))
            scr.blit(t, t.get_rect(center=rect.center))

        for a in sim.amrs:
            col = AMR_COLORS[a.idx % len(AMR_COLORS)]
            # 앞으로 갈 경로
            cells = []
            if a.schedule:
                cells = [n for _, n, _ in a.schedule]
            elif a.path:
                cells = list(a.path)
            if a.goal is not None:
                pts = [self.amr_pos(a)] + [self.px(*p) for p in cells]
                if len(pts) > 1:
                    pg.draw.lines(scr, col, False, pts, 2)
                gx, gy = self.px(*a.goal)
                d = c * 0.25
                pg.draw.line(scr, col, (gx - d, gy - d), (gx + d, gy + d), 3)
                pg.draw.line(scr, col, (gx - d, gy + d), (gx + d, gy - d), 3)
        for a in sim.amrs:
            col = AMR_COLORS[a.idx % len(AMR_COLORS)]
            x, y = self.amr_pos(a)
            rad = c * 0.42
            waiting = a.waiting or a.restart_left > 0
            if waiting:
                pg.draw.circle(scr, (230, 30, 30), (x, y), rad + 4, 3)
            pg.draw.circle(scr, col, (x, y), rad)
            if a.loaded:
                pg.draw.rect(scr, (250, 230, 120), pg.Rect(x - rad * 0.45, y - rad * 0.45, rad * 0.9, rad * 0.9))
            t = small.render(str(a.idx + 1), True, TEXT if a.loaded else (255, 255, 255))
            scr.blit(t, t.get_rect(center=(x, y)))

        # 상태 패널
        top = self.y0 + HEIGHT * c + 8
        scr.blit(mid.render(LABEL.get(sim.name, sim.name), True, TEXT), (self.x0, self.y0 - 26))
        waits = sum(a.stats.wait for a in sim.amrs) * P.TICK
        stops = sum(a.stats.stops for a in sim.amrs)
        dist = sum(a.stats.distance for a in sim.amrs)
        lines = [
            f"t = {sim.now * P.TICK:8.1f}s   Done {sim.done}/{len(sim.jobs)}   Queue {len(sim.queue)}",
            f"distance {dist:,} m   wait {waits:,.0f}s   stops {stops}   bottleneck wait {sim.zone_wait * P.TICK:,.0f}s",
        ]
        for a in sim.amrs:
            lines.append(f"{a.status():<22} wait {a.stats.wait * P.TICK:6.0f}s  stops {a.stats.stops:4d}  jobs {a.stats.jobs}")
        for i, ln in enumerate(lines):
            col = AMR_COLORS[(i - 2) % len(AMR_COLORS)] if i >= 2 else (TEXT if i == 0 else SUB)
            scr.blit(small.render(ln, True, col), (self.x0, top + i * 18))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--case", nargs="*", default=["CASE1", "CASE2"])
    ap.add_argument("--start", type=float, default=0.0, help="이 시각(초)까지 화면 없이 계산 후 재생 시작")
    ap.add_argument("--speed", type=float, default=10.0, help="배속 (시뮬레이션 초 / 실제 초)")
    ap.add_argument("--cell", type=int, default=24, help="격자 1칸 픽셀")
    ap.add_argument("--seed", type=int, default=P.SEED)
    ap.add_argument("--shot", default=None, help="창 없이 --start 시각 화면을 PNG 로 저장하고 종료")
    args = ap.parse_args()

    if args.shot:
        os.environ["SDL_VIDEODRIVER"] = "dummy"
    import pygame as pg

    jobs = generate_jobs(args.seed)
    sims = [make_sim(c, jobs) for c in args.case]
    start_tick = int(args.start / P.TICK)
    if start_tick:
        print(f"{args.start:.0f}초까지 계산 중…")
        for s in sims:
            while s.now < start_tick and not s.finished:
                s.tick()

    pg.init()
    c = args.cell
    pane_w, pane_h = WIDTH * c, HEIGHT * c
    margin, head, foot = 20, 40, 18 * 6 + 20
    W = margin + len(sims) * (pane_w + margin)
    H = head + pane_h + foot
    scr = pg.display.set_mode((W, H))
    pg.display.set_caption("AMR warehouse simulation")
    fonts = (pg.font.SysFont("dejavusans", int(c * 0.9), bold=True),
             pg.font.SysFont("dejavusans", 17, bold=True),
             pg.font.SysFont("dejavusansmono", 13))
    panes = [Pane(s, margin + i * (pane_w + margin), head, c) for i, s in enumerate(sims)]

    def render():
        scr.fill(BG)
        for p in panes:
            p.draw(pg, scr, fonts)
        info = fonts[2].render(f"speed x{args.speed:g}   SPACE pause  UP/DOWN speed  RIGHT +1s  S screenshot  ESC quit",
                               True, SUB)
        scr.blit(info, (margin, H - 18))

    if args.shot:
        render()
        pg.image.save(scr, args.shot)
        print(f"저장: {args.shot}")
        return

    clock = pg.time.Clock()
    paused, fps, carry, shot_no = False, 30, 0.0, 0
    running = True
    while running:
        for e in pg.event.get():
            if e.type == pg.QUIT:
                running = False
            elif e.type == pg.KEYDOWN:
                if e.key == pg.K_ESCAPE:
                    running = False
                elif e.key == pg.K_SPACE:
                    paused = not paused
                elif e.key == pg.K_UP:
                    args.speed = min(args.speed * 2, 2000)
                elif e.key == pg.K_DOWN:
                    args.speed = max(args.speed / 2, 0.25)
                elif e.key == pg.K_RIGHT and paused:
                    for s in sims:
                        for _ in range(10):
                            if not s.finished:
                                s.tick()
                elif e.key == pg.K_s:
                    shot_no += 1
                    pg.image.save(scr, f"screenshot_{shot_no}.png")
        if not paused:
            carry += args.speed / P.TICK / fps
            n = int(carry)
            carry -= n
            for s in sims:
                for _ in range(n):
                    if not s.finished:
                        s.tick()
        render()
        pg.display.flip()
        clock.tick(fps)
    pg.quit()


if __name__ == "__main__":
    main()
