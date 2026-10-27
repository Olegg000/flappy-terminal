#!/usr/bin/env python3
"""Flappy Bird for the terminal - one file, standard library only (Python 3.9+).

    python3 flappy.py [--ascii] [--no-color] [--fps N] [--seed N] [--easy | --hard] [--demo]
    python3 flappy.py --selftest          # headless model checks, no terminal needed

Space / Up / w / k  flap          p  pause            m  sound on/off
Left / Right        difficulty    d  watch the demo   t  back to the title
Space / r           restart after a crash             q / Esc  quit

Game (state machine + simulation; knows the screen size, never curses) is drawn
by Renderer onto a Canvas (a clipped curses window).  Theme and Glyphs resolve
colour roles and character sets; Bot is the demo autopilot.
"""
from __future__ import annotations

import argparse
import codecs
import json
import locale
import math
import os
import random
import sys
import time
from typing import Dict, List, Optional, Tuple

try:
    import curses
except ImportError:                      # Windows ships Python without curses
    sys.exit("flappy: the curses module is missing. On Windows install it with:  py -m pip install windows-curses")

# --------------------------------------------------------------------------- #
# Tunables (units: terminal rows / columns / seconds)
# --------------------------------------------------------------------------- #
PHYSICS_HZ = 60                 # fixed physics rate, independent of --fps
PHYSICS_DT = 1.0 / PHYSICS_HZ
MAX_STEPS_PER_FRAME = 6         # catch-up steps per frame (more at a low --fps) before stalled time is dropped
GRAVITY = 50.0                  # rows/s^2
FLAP_VY = -15.5                 # rows/s, SET by a flap: apex 2.4 rows after 0.31 s (snappy)
MAX_FALL_VY = 24.0              # rows/s terminal velocity: a full-field fall takes ~1.3 s (floaty but deadly)
FLAP_RISE = FLAP_VY * FLAP_VY / (2.0 * GRAVITY)   # rows gained by one flap
CLIMB_RATE = 8.0                # rows/s a player climbs with chained flaps (~4 taps/s), for the fairness rule
BIRD_X_FRAC = 0.24              # bird column as a fraction of the width
BIRD_W, BIRD_H = 6, 2
HOVER_BOB = 0.6                 # rows the idle bird bobs above and below its hover height
PIPE_W = 7                      # pipe body width; the cap is one column wider on each side
CAP_W, CAP_H = PIPE_W + 2, 2
FIRST_PIPE_DIST = 72            # columns from the bird to the first pipe, whatever the terminal width
GAP_TOP_MIN = 1 + CAP_H         # highest first free row of a gap (one pipe row plus the cap stay above)
GAP_MARGIN_BOTTOM = 1           # pipe rows kept below the lowest gap (plus the cap)
MAX_GAP_DELTA = 6               # hard cap on the gap shift between neighbours (see Game.gap_shift_limits)
SPEED_PER_POINT = 0.2           # columns/s gained per point (capped per difficulty)
GAP_SHRINK_PER_POINT = 0.04     # rows of gap lost per point (floored per difficulty)
GROUND_H = 3
MIN_COLS, MIN_ROWS = 50, 16
NIGHT_EVERY = 10                # points between day/night switches
MEDALS = ((40, "platinum"), (30, "gold"), (20, "silver"), (10, "bronze"))
DEATH_ANIM_MAX = 1.6
SHAKE_TIME, FLASH_TIME, TOAST_TIME = 0.35, 0.10, 1.6
ATTRACT_AFTER = 12.0            # idle seconds on the title before the demo starts by itself
PARALLAX = (0.18, 0.38, 0.62)   # clouds, far skyline, near bushes (fraction of pipe speed)
DIFFICULTIES = {                # name: (speed, max speed, gap rows, min gap rows, columns between pipes)
    "EASY": (15.0, 19.0, 8.0, 6.0, 30),     # a pipe every 2.0 s -> 1.6 s
    "NORMAL": (19.0, 23.0, 7.0, 6.0, 27),   # a pipe every 1.4 s -> 1.2 s, the original's cadence
    "HARD": (21.0, 25.0, 6.0, 5.0, 27),     # a pipe every 1.3 s -> 1.1 s, faster and tighter
}
DIFF_ORDER = ("EASY", "NORMAL", "HARD")
SCORE_FILE_DEFAULT = "~/.flappy_term.json"
WINDOWS = sys.platform == "win32"     # windows-curses (PDCurses): Unicode always works, resizes need resize_term
BEST_SCORE_CAP = 1000000        # a stored best above this can only be a corrupt file
FLAP_KEYS = (ord(" "), curses.KEY_UP, ord("w"), ord("W"), ord("k"), ord("K"))
QUIT_KEYS = (ord("q"), ord("Q"), 27, 3)  # 3 = Ctrl-C when the console delivers it as a key (Windows)
ARROW_KEYS = {ord("A"): curses.KEY_UP, ord("B"): curses.KEY_DOWN, ord("C"): curses.KEY_RIGHT, ord("D"): curses.KEY_LEFT}

# --------------------------------------------------------------------------- #
# Block-letter font (5 pixel rows per glyph, '#' = ink), glyph sets, bird art
# --------------------------------------------------------------------------- #
_FONT_SRC = """
A .###. #...# ##### #...# #...#   B ####. #...# ####. #...# ####.   D ####. #...# #...# #...# ####.
E ##### #.... ####. #.... #####   F ##### #.... ####. #.... #....   G .#### #.... #.### #...# .####
I ##### ..#.. ..#.. ..#.. #####   L #.... #.... #.... #.... #####   M #...# ##.## #.#.# #...# #...#
O .###. #...# #...# #...# .###.   P ####. #...# ####. #.... #....   R ####. #...# ####. #..#. #...#
S .#### #.... .###. ....# ####.   T ##### ..#.. ..#.. ..#.. ..#..   U #...# #...# #...# #...# .###.
V #...# #...# #...# .#.#. ..#..   Y #...# .#.#. ..#.. ..#.. ..#..   0 .##. #..# #..# #..# .##.
1 .#.. ##.. .#.. .#.. ####   2 ###. ...# .##. #... ####   3 ###. ...# .##. ...# ###.   4 #..# #..# #### ...# ...#
5 #### #... ###. ...# ###.   6 .### #... ###. #..# .##.   7 #### ...# ..#. .#.. .#..   8 .##. #..# .##. #..# .##.
9 .##. #..# .### ...# ###.
"""
_words = _FONT_SRC.split()
FONT: Dict[str, List[str]] = {" ": ["  "] * 5}
FONT.update((_words[i], _words[i + 1:i + 6]) for i in range(0, len(_words), 6))

_GLYPHS = {   # role: (Unicode, ASCII)
    "full": ("█", "#"), "upper": ("▀", '"'), "lower": ("▄", "."), "shadow": ("░", ":"), "eye": ("•", "O"),
    "pipe_hi": ("▌", "|"), "pipe_shade": ("▒█", "#|"), "cap_hi": ("▌", "["), "cap_shade": ("▒█", "=]"),
    "cap_body": ("█", "="), "grass": ("█▓▒░░▒▓ ", "////  "),
    "dirt1": ("░   ▒  ░    ▒   ", ".   :  .    :   "), "dirt2": ("   ░    ▒ ░     ", "   .    : .     "),
    "cloud_big": (("  ▄███▄▄  ", "▀████████▀"), ("  .--.--. ", " (______) ")),
    "cloud_small": ((" ▄██▄ ", "▀████▀"), (" .--. ", "(____)")),
    "sun": ((" ▄██▄ ", "██████", " ▀██▀ "), (" .--. ", "(    )", " `--' ")),
    "moon": ((" ▄██▀", "███  ", " ▀██▄"), (" .-'", "(   ", " `-.")),
    "medal": ((" ▄█▄ ", "█████", " ▀█▀ "), (" .-. ", "(   )", " `-' ")),
    "stars": ("·✦·", ".+."), "puff": ("∘°·", "o*."), "box": ("╔═╗║╚╝", "+-+|++"), "arrows": ("◂▸", "<>"),
    "hill_ramp": ("▁▂▃▄▅▆▇█", "....%%%%"), "bush_ramp": ("▁▂▃▄▅▆▇█", "wwwwWWWW"),
}


class Glyphs:
    """Character set used for drawing: Unicode blocks, or a pure-ASCII fallback (same role names)."""

    def __init__(self, ascii_only: bool) -> None:
        self.ascii = ascii_only
        for role, (uni, asc) in _GLYPHS.items():
            setattr(self, role, asc if ascii_only else uni)


# Bird pixel art: 4 pixel rows x 6 columns per wing frame; letters are colour roles
# (y body, o wing, e eye, b beak), '.' is transparent.  The beak pixel is added per
# pose, then everything is composited into 2 rows of half blocks.
_BIRD_PX = {0: ("..yyy.", "ooyye.", ".yyyy.", "..yyy."),     # wing up
            1: ("..yyy.", ".yyye.", "ooyyy.", "..yyy."),     # wing level
            2: ("..yyy.", ".yyye.", ".yyyy.", "ooyyy.")}     # wing down
_BEAK_ROW = {"up": 1, "level": 2, "down": 3}
_ROLE_OF = {"y": "bird", "o": "wing", "e": "eye", "b": "beak"}
Sprite = List[Tuple[int, int, str, str, str]]   # (dy, dx, char, fg role, bg role)


def build_bird_sprites(glyphs: Glyphs) -> Dict[Tuple[str, int], Sprite]:
    """Composite the bird pixel art into cell sprites for every (pose, wing) pair."""
    sprites: Dict[Tuple[str, int], Sprite] = {}
    for pose, beak_row in _BEAK_ROW.items():
        for wing, rows in _BIRD_PX.items():
            cells: Sprite = []
            if glyphs.ascii:
                top = " .ooO" + ("/" if pose == "up" else " ")
                bot = "(" + "^-v"[wing] + "oo)" + {"level": ">", "down": "\\"}.get(pose, " ")
                for dy, (line, roles) in enumerate(((top, " yyyeb"), (bot, "yoyyyb"))):
                    cells.extend((dy, dx, ch, _ROLE_OF[roles[dx]], "sky") for dx, ch in enumerate(line) if ch != " ")
            else:
                px = [list(r) for r in rows]
                px[beak_row][5] = "b"
                for dy in range(BIRD_H):
                    for dx in range(BIRD_W):
                        a, b = px[2 * dy][dx], px[2 * dy + 1][dx]
                        if "e" in (a, b):
                            cells.append((dy, dx, glyphs.eye, "eye", ""))
                        elif a == b != ".":
                            cells.append((dy, dx, glyphs.full, _ROLE_OF[a], "sky"))
                        elif a == "." and b != ".":
                            cells.append((dy, dx, glyphs.lower, _ROLE_OF[b], "sky"))
                        elif a != ".":
                            cells.append((dy, dx, glyphs.upper, _ROLE_OF[a], "sky" if b == "." else _ROLE_OF[b]))
            sprites[(pose, wing)] = cells
    return sprites


# --------------------------------------------------------------------------- #
# Colours
# --------------------------------------------------------------------------- #
SKY = -2   # sentinel: "the sky colour of the current palette"
_B, _D, _R = curses.A_BOLD, curses.A_DIM, curses.A_REVERSE
_K, _W, _Y, _G, _C, _RD = (curses.COLOR_BLACK, curses.COLOR_WHITE, curses.COLOR_YELLOW,
                           curses.COLOR_GREEN, curses.COLOR_CYAN, curses.COLOR_RED)
# role: (day fg, day bg, night fg, night bg,  16-colour fg, bg,  attrs,  monochrome attrs)
# -1 keeps the terminal's default colour; SKY resolves to the palette's sky colour.
PALETTE = {
    "sky":       (117, 117,  17,  17,  -1, -1, 0, 0),
    "flash":     (231, 231, 231, 231,  -1, -1, _R, _R),
    "cloud":     (231, SKY,  61, SKY,  _C, -1, 0, _D),
    "sun":       (220, SKY, 254, SKY,  _Y, -1, _B, _B),      # also the moon and the stars
    "hill_far":  (115, SKY,  23, SKY,  _C, -1, _D, _D),
    "hill_near": ( 71, SKY,  22, SKY,  _G, -1, _D, _D),
    "pipe_hi":   (118, SKY, 112, SKY,  _G, -1, _B, _B),
    "pipe":      ( 40, SKY,  34, SKY,  _G, -1, 0, 0),
    "pipe_dark": ( 28, SKY,  22, SKY,  _G, -1, _D, _D),
    "bird":      (220, SKY, 220, SKY,  _Y, -1, _B, _B),      # also the logo
    "wing":      (214, SKY, 214, SKY,  _Y, -1, 0, 0),
    "beak":      (208, SKY, 208, SKY, _RD, -1, _B, _B),
    "eye":       ( 16, 231,  16, 231,  _K, _W, 0, _B),
    "grass":     (154,  70, 106,  58,  _G, -1, 0, 0),
    "dirt":      (180, 137, 137,  94,  _Y, -1, _D, _D),
    "text":      ( 24, SKY, 153, SKY,  -1, -1, 0, 0),
    "text_hi":   (231, SKY, 231, SKY,  -1, -1, _B, _B),      # also the score and the '+1' popup
    "logo_sh":   (130, SKY,  94, SKY,  _Y, -1, _D, _D),
    "accent":    (208, SKY, 214, SKY, _RD, -1, _B, _B),
    "panel":     ( 16, 187,  16, 187,  _K, _W, 0, _R),
    "panel_dim": (101, 187, 101, 187,  _K, _W, _D, _R | _D),
    "panel_hi":  ( 88, 187,  88, 187, _RD, _W, _B, _R | _B),
    "bronze":    (130, 187, 130, 187, _RD, _W, _B, _R),
    "silver":    (245, 187, 245, 187,  _K, _W, _B, _R),
    "gold":      (178, 187, 178, 187,  _Y, _W, _B, _R | _B),
    "platinum":  ( 31, 187,  31, 187,  _C, _W, _B, _R | _B),
}


class Theme:
    """Resolves colour roles to curses attributes for the terminal at hand (256 colours, 16, or mono)."""

    def __init__(self, want_color: bool) -> None:
        self.night, self.mode, self._defaults_ok = False, "mono", False
        self._pairs: Dict[Tuple[int, int], int] = {}
        if want_color and curses.has_colors():
            try:
                curses.use_default_colors()
                self._defaults_ok = True
            except curses.error:
                pass
            self.mode = "256" if curses.COLORS >= 256 else "16"
        self.painted = self.mode == "256"   # whether the sky is painted as a background colour

    def _pair(self, fg: int, bg: int) -> int:
        """Return the attribute for a (fg, bg) pair, allocating it lazily."""
        key = (fg, bg)
        if key not in self._pairs:
            if not self._defaults_ok:
                fg, bg = (curses.COLOR_WHITE if fg < 0 else fg), (curses.COLOR_BLACK if bg < 0 else bg)
            idx, self._pairs[key] = len(self._pairs) + 1, 0
            if idx < min(curses.COLOR_PAIRS, 256):
                try:
                    curses.init_pair(idx, fg, bg)
                    self._pairs[key] = curses.color_pair(idx)
                except curses.error:
                    pass
        return self._pairs[key]

    def _colors(self, role: str) -> Tuple[int, int, int]:
        """(fg, bg, attrs) of a role in the current palette with SKY resolved."""
        row = PALETTE[role]
        if self.mode != "256":
            return row[4], row[5], row[6]
        fg, bg = row[2:4] if self.night else row[0:2]
        sky = PALETTE["sky"][2 if self.night else 0]
        return (sky if fg == SKY else fg), (sky if bg == SKY else bg), row[6]

    def attr(self, role: str, over: Optional[str] = None) -> int:
        """Attribute of a role; with `over`, its colour on top of another role's colour (half-block art)."""
        if self.mode == "mono":
            return PALETTE[role][7]
        fg, bg, extra = self._colors(role)
        return self._pair(fg, self._colors(over)[0] if over else bg) | extra


# --------------------------------------------------------------------------- #
# Drawing surface and block text
# --------------------------------------------------------------------------- #
class Canvas:
    """Thin wrapper over the curses window: clipped writes that never raise."""

    def __init__(self, scr: "curses.window") -> None:
        self.scr = scr
        self.h, self.w = scr.getmaxyx()
        self.dx = 0          # screen-shake offset applied to put()

    def refresh_size(self) -> bool:
        h, w = self.scr.getmaxyx()
        changed = (h, w) != (self.h, self.w)
        self.h, self.w = h, w
        return changed

    def put(self, y: int, x: int, s: str, attr: int = 0) -> None:
        x += self.dx
        if y < 0 or y >= self.h or x >= self.w or not s:
            return
        if x < 0:
            s, x = s[-x:], 0
        s = s[: self.w - x]
        if not s:
            return
        try:
            self.scr.addstr(y, x, s, attr)
        except curses.error:
            pass                      # bottom-right cell: written, the cursor could not advance
        except UnicodeEncodeError:    # the locale lied about UTF-8: degrade instead of dying
            try:
                self.scr.addstr(y, x, "?" * len(s), attr)
            except curses.error:
                pass

    def put_runs(self, y: int, x: int, s: str, attr: int = 0) -> None:
        """Like put(), but spaces are transparent."""
        i, n = 0, len(s)
        while i < n:
            j = i
            while j < n and s[j] != " ":
                j += 1
            if j > i:
                self.put(y, x + i, s[i:j], attr)
            i = j + 1

    def center(self, y: int, s: str, attr: int = 0) -> None:
        self.put(y, (self.w - len(s)) // 2, s, attr)


class BigText:
    """Renders strings in the block font: 5 rows of full blocks or 3 rows of half blocks."""

    def __init__(self, glyphs: Glyphs) -> None:
        self.g = glyphs

    def rows(self, text: str, half: bool, xscale: int = 1) -> List[str]:
        bitmap = ["" for _ in range(5)]
        for ch in text.upper():
            glyph = FONT.get(ch, FONT[" "])
            for r in range(5):
                bitmap[r] += glyph[r] + "."
        bitmap, g = [row[:-1] for row in bitmap], self.g
        if not half or g.ascii:
            out = ["".join(g.full if c == "#" else " " for c in row) for row in bitmap]
        else:
            pairs = ((bitmap[0], bitmap[1]), (bitmap[2], bitmap[2]), (bitmap[3], bitmap[4]))
            out = ["".join(g.full if a == b == "#" else g.upper if a == "#" else g.lower if b == "#" else " "
                           for a, b in zip(top, bot)) for top, bot in pairs]
        return ["".join(c * xscale for c in row) for row in out] if xscale > 1 else out

    def width(self, text: str, xscale: int = 1) -> int:
        return len(self.rows(text, True, xscale)[0])

    def height(self, half: bool) -> int:
        return 3 if half and not self.g.ascii else 5

    def draw(self, cv: Canvas, y: int, text: str, attr: int, x: Optional[int] = None, shadow: Optional[int] = None,
             half: bool = True, backing: Optional[int] = None, xscale: int = 1, pad: int = 0) -> int:
        """Draw text (centred when x is None) with optional drop shadow and opaque backing (`pad` extra blank
        columns on each side keep neighbouring art off the glyphs); returns the rows used."""
        lines = self.rows(text, half, xscale)
        if x is None:
            x = (cv.w - len(lines[0])) // 2
        if backing is not None:                      # three passes: backing, then shadow, then glyphs,
            for i, line in enumerate(lines):         # so a row's backing never wipes the row above's shadow
                cv.put(y + i, x - pad, " " * (len(line) + 2 * pad), backing)
        if shadow is not None:
            for i, line in enumerate(lines):
                cv.put_runs(y + i + 1, x + 1, "".join(" " if c == " " else self.g.shadow for c in line), shadow)
        for i, line in enumerate(lines):
            cv.put_runs(y + i, x, line, attr)
        return len(lines)


# --------------------------------------------------------------------------- #
# World objects
# --------------------------------------------------------------------------- #
class Bird:
    """The player: float vertical position, velocity, wing animation and pose."""

    def __init__(self, x: int, y: float, sprites: Dict[Tuple[str, int], Sprite]) -> None:
        self.x, self.y, self.vy = x, y, 0.0
        self.sprites = sprites
        self.wing_t = self.flap_anim = self.bob_t = self.tumble = 0.0

    def flap(self) -> None:
        self.vy, self.flap_anim = FLAP_VY, 0.3

    def update(self, dt: float, floor: float) -> None:
        """Gravity with a capped fall speed; the ceiling clamps without killing."""
        self.vy = min(self.vy + GRAVITY * dt, MAX_FALL_VY)
        self.y += self.vy * dt
        if self.y < 0.0:
            self.y, self.vy = 0.0, max(self.vy, 0.0)
        if self.y > floor:
            self.y, self.vy = floor, 0.0
        self.flap_anim = max(0.0, self.flap_anim - dt)
        self.wing_t += (16.0 if self.flap_anim > 0 else 7.0 if self.vy < 0 else 2.5) * dt

    def hover(self, dt: float, base_y: float) -> None:
        """Idle bob used on the title and GET READY screens."""
        self.bob_t += dt
        self.y, self.vy = base_y + HOVER_BOB * math.sin(self.bob_t * 3.2), 0.0
        self.wing_t += 5.0 * dt

    @property
    def row(self) -> int:
        return int(self.y + 0.5)

    def sprite(self) -> Sprite:
        pose = "down" if self.tumble > 0 or self.vy > 8.0 else "up" if self.vy < -3.0 else "level"
        return self.sprites[(pose, (0, 1, 2, 1)[int(self.wing_t) % 4])]

    def cells(self) -> List[Tuple[int, int]]:
        """Hitbox cells: body, eye and beak; the trailing wing tip is forgiven."""
        r0 = self.row
        return [(r0 + dy, self.x + dx) for dy, dx, _, fg, _ in self.sprite() if fg != "wing"]


class Pipe:
    """A top/bottom pipe pair with a gap of free rows [gap_top, gap_bot)."""

    def __init__(self, x: float, gap_top: int, gap_bot: int) -> None:
        self.x, self.gap_top, self.gap_bot, self.passed = x, gap_top, gap_bot, False

    @property
    def col(self) -> int:
        return int(math.floor(self.x))

    def hits(self, cells: List[Tuple[int, int]]) -> bool:
        x0 = self.col
        for r, c in cells:
            if self.gap_top <= r < self.gap_bot:
                continue
            cap = self.gap_top - CAP_H <= r < self.gap_bot + CAP_H
            left = x0 - 1 if cap else x0
            if left <= c < left + (CAP_W if cap else PIPE_W):
                return True
        return False


class Background:
    """Parallax layers: stars, sun/moon, clouds, a far city skyline and near bushes."""

    PERIOD = 480

    def __init__(self, rng: random.Random, glyphs: Glyphs) -> None:
        self.rng, self.g = rng, glyphs
        self.offsets = [0.0, 0.0, 0.0]
        self.stars = [(rng.random(), rng.randint(0, 9), rng.random() * 6.0) for _ in range(36)]
        self.clouds: List[list] = []              # [x, row, big]
        self.span, self.hill_rows, self.bush_rows = 160, 3, 2
        self.hill_lines: List[str] = []
        self.bush_lines: List[str] = []
        self.sky_prof = self._profile(flat=True)
        self.bush_prof = self._profile(flat=False)

    def _profile(self, flat: bool) -> List[float]:
        """Periodic height profile (fractions of the band): flat-topped buildings or rounded bumps."""
        prof: List[float] = []
        while len(prof) < self.PERIOD:
            if flat:
                prof.extend([self.rng.choice((0.25, 0.4, 0.5, 0.6, 0.75, 0.9, 1.0))] * self.rng.randint(3, 8))
            else:
                width, peak = self.rng.randint(5, 12), self.rng.uniform(0.5, 1.0)
                prof.extend(peak * math.sin(math.pi * (i + 0.5) / width) for i in range(width))
            prof.extend([0.0] * self.rng.randint(0, 2 if flat else 1))
        return prof[: self.PERIOD]

    @staticmethod
    def _render(prof: List[float], rows: int, ramp: str) -> List[str]:
        """Pre-render a profile into row strings with eighth-row precision (ramp[7] is a full cell)."""
        heights = [max(0, min(8 * rows, int(round(f * 8 * rows)))) for f in prof]
        lines = []
        for r in range(rows):
            lo = 8 * (rows - 1 - r)
            lines.append("".join(ramp[7] if h >= lo + 8 else ramp[h - lo - 1] if h > lo else " " for h in heights))
        return lines

    def layout(self, w: int, ground_y: int) -> None:
        self.span = max(w + 40, 120)
        self.hill_rows = max(1, min(4, (ground_y - 8) // 4))
        self.bush_rows = 1 if ground_y < 14 else 2
        self.hill_lines = self._render(self.sky_prof, self.hill_rows, self.g.hill_ramp)
        self.bush_lines = self._render(self.bush_prof, self.bush_rows, self.g.bush_ramp)
        while len(self.clouds) < max(3, w // 22):
            self.clouds.append([self.rng.uniform(0, self.span), 0, self.rng.random() < 0.5])
        top_rows = max(2, ground_y - self.hill_rows - 4)
        for c in self.clouds:
            c[1] = min(c[1], top_rows - 1) if c[1] else self.rng.randint(1, top_rows - 1)

    def update(self, dt: float, speed: float) -> None:
        for i, f in enumerate(PARALLAX):
            self.offsets[i] += speed * f * dt
        for c in self.clouds:
            if c[0] - self.offsets[0] + 10 < 0:
                c[0] += self.span
                if self.rng.random() < 0.5:
                    c[1] = self.rng.randint(1, max(1, c[1] + 2))

    def slice(self, lines: List[str], offset: float, w: int) -> List[str]:
        o = int(offset) % self.PERIOD
        return [(line[o:] + line)[:w] if o + w > self.PERIOD else line[o:o + w] for line in lines]


class Particle:
    """A flap puff, or a floating text popup when `text` is set."""

    def __init__(self, x: float, y: float, vx: float, vy: float, life: float, text: str = "") -> None:
        self.x, self.y, self.vx, self.vy, self.life, self.age, self.text = x, y, vx, vy, life, 0.0, text


class Effects:
    """Flap puffs, '+1' popups, death shake and flash."""

    def __init__(self, rng: random.Random) -> None:
        self.rng = rng
        self.items: List[Particle] = []
        self.shake = self.flash = 0.0

    def clear(self) -> None:
        self.items, self.shake, self.flash = [], 0.0, 0.0

    def puff(self, x: float, y: float) -> None:
        for _ in range(3):
            self.items.append(Particle(x, y, -self.rng.uniform(8, 16), self.rng.uniform(-3, 4), 0.4))

    def popup(self, x: float, y: float, text: str) -> None:
        self.items.append(Particle(x, y, 0.0, -2.5, 0.7, text))

    def update(self, dt: float, scroll: float) -> None:
        self.shake, self.flash = max(0.0, self.shake - dt), max(0.0, self.flash - dt)
        for p in self.items:
            p.age += dt
            p.x += (p.vx - (0.0 if p.text else scroll)) * dt
            p.y += p.vy * dt
        self.items = [p for p in self.items if p.age < p.life]


# --------------------------------------------------------------------------- #
# Autopilot and persistence
# --------------------------------------------------------------------------- #
class Bot:
    """Autopilot for --demo: flap when the bird would sink below the flap line of the next gap.  Past a
    gap's centre it already works towards the gap after it: climbing inside the current gap when the
    next one is higher, or diving early when free fall stays clear of the lower lip."""

    @staticmethod
    def flap_line(p: Pipe) -> float:
        """Lowest bird top allowed inside a gap: a hop above it still clears the upper cap."""
        return p.gap_bot - BIRD_H - max(0.0, (p.gap_bot - p.gap_top - 5) / 2.0)

    @classmethod
    def wants_flap(cls, bird: Bird, pipes: List[Pipe], ground_y: int, speed: float) -> bool:
        ahead = [p for p in pipes if p.x + PIPE_W + 1 > bird.x]
        if not ahead:
            target = ground_y * 0.45 - 1.0
        else:
            cur = ahead[0]
            target = cls.flap_line(cur)
            if len(ahead) > 1 and bird.x + BIRD_W / 2.0 > cur.x + PIPE_W / 2.0:
                nxt = cls.flap_line(ahead[1])
                if nxt > target:                                   # the next gap is lower
                    t = max(0.0, (cur.x + PIPE_W - bird.x) / speed)   # until this pipe is behind the bird
                    if bird.y + bird.vy * t + 0.5 * GRAVITY * t * t < cur.gap_bot - BIRD_H + 0.4:
                        return False                               # free fall stays clear of the lip: dive
                    target = cur.gap_bot - BIRD_H + 0.3            # else sink to the lip and hop there
                else:                                              # higher: climb short of the upper lip
                    target = max(cur.gap_top + FLAP_RISE + 0.6, nxt)
        if bird.y + (bird.vy + GRAVITY * PHYSICS_DT) * PHYSICS_DT < target:
            return False                                           # still above the line next tick
        return bird.vy > (-9.0 if bird.y - target > 1.5 else -3.0)  # chain flaps fast when far below


class ScoreStore:
    """Best score per difficulty in a small JSON file; silent on any I/O problem, atomic on write."""

    def __init__(self, path: Optional[str] = None) -> None:
        self.path = path or os.path.expanduser(os.environ.get("FLAPPY_SCORE_FILE") or SCORE_FILE_DEFAULT)
        self.best: Dict[str, int] = {}
        try:
            with open(self.path, "r", encoding="utf-8") as fh:
                table = json.loads(fh.read(65536)).get("best")   # bounded read: a huge file cannot stall startup
        except Exception:                                        # noqa: BLE001 - any damage means "no best yet"
            table = None
        if isinstance(table, (int, float)) and not isinstance(table, bool):
            table = {"NORMAL": table}                    # legacy single-number file
        for name, val in (table.items() if isinstance(table, dict) else ()):
            if name in DIFFICULTIES and isinstance(val, (int, float)) and not isinstance(val, bool):
                try:
                    val = int(val)
                except (OverflowError, ValueError):   # 1e400 parses as inf, NaN is a float too
                    continue
                if 0 <= val <= BEST_SCORE_CAP:
                    self.best[name] = val

    def get(self, name: str) -> int:
        return self.best.get(name, 0)

    def record(self, name: str, score: int) -> bool:
        """Store a new best for a difficulty; returns True when it improved."""
        if score <= self.get(name):
            return False
        self.best[name] = int(score)
        tmp = "%s.%d.tmp" % (self.path, os.getpid())
        try:
            if os.path.dirname(self.path):
                os.makedirs(os.path.dirname(self.path), exist_ok=True)
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump({"best": self.best}, fh)
            os.replace(tmp, self.path)
        except OSError:
            try:
                os.unlink(tmp)
            except OSError:
                pass
        return True


def medal_for(score: int) -> Optional[str]:
    return next((name for threshold, name in MEDALS if score >= threshold), None)


# --------------------------------------------------------------------------- #
# Game logic
# --------------------------------------------------------------------------- #
TITLE, READY, PLAYING, DYING, OVER = range(5)


class Game:
    """State machine, world simulation, scoring and key handling; knows the screen size, never curses."""

    def __init__(self, h: int, w: int, theme: Theme, glyphs: Glyphs, opts: argparse.Namespace,
                 store: ScoreStore) -> None:
        self.theme, self.g, self.store = theme, glyphs, store
        self.sprites = build_bird_sprites(glyphs)
        seed = opts.seed if opts.seed is not None else random.randrange(1 << 30)
        self.rng = random.Random(seed)                 # pipes only: --seed gives the same world whatever you do
        self.bg = Background(random.Random(seed + 1), glyphs)
        self.fx = Effects(random.Random(seed + 2))
        self.demo = bool(opts.demo)
        self.diff = DIFF_ORDER.index(opts.difficulty)
        self.sound = self.paused = self.too_small = self.new_best = False
        self.running = True
        self.pause_reason = self.toast = ""
        self.time = self.toast_t = self.title_t = 0.0
        self.h, self.w = h, w
        self.pipes: List[Pipe] = []
        self.bird = Bird(0, 0.0, self.sprites)
        self.bird_x, self.spawn_x = 0, 0.0
        self.state = READY if self.demo else TITLE
        self.layout()
        self.reset_round()

    @property
    def best(self) -> int:
        return self.store.get(DIFF_ORDER[self.diff])

    # -- layout ----------------------------------------------------------------
    def layout(self) -> None:
        h, w = self.h, self.w
        self.too_small = w < MIN_COLS or h < MIN_ROWS
        self.ground_y = max(6, h - GROUND_H)
        self.hover_y = (self.ground_y - BIRD_H) * 0.45
        self.bg.layout(w, self.ground_y)
        bird_x = max(2, int(w * BIRD_X_FRAC))
        dx = bird_x - self.bird_x               # the bird keeps its column fraction: the world shifts with it
        self.bird_x = self.bird.x = bird_x
        for p in self.pipes:
            p.x += dx
        for item in self.fx.items:
            item.x += dx
        self.spawn_x = max(float(w + 2), self.spawn_x + dx) if self.state == PLAYING else self.first_spawn_x()
        if self.too_small:
            return                   # the world is frozen; clamp against the real size when it is back
        lift = 3 if self.state in (READY, PLAYING) else 0    # never resize straight into the ground
        self.bird.y = max(0.0, min(self.bird.y, float(self.ground_y - BIRD_H - lift)))
        for p in self.pipes:
            gap = p.gap_bot - p.gap_top
            p.gap_top = max(GAP_TOP_MIN, min(p.gap_top, self.gap_top_max(gap)))
            p.gap_bot = min(p.gap_top + gap, self.ground_y - 1)
            if self.state == PLAYING and p.hits(self.bird.cells()):   # a clamped gap slid off the bird
                self.bird.y = float(max(p.gap_top, min(self.bird.row, p.gap_bot - BIRD_H)))

    def first_spawn_x(self) -> float:
        """Column of a round's first pipe: FIRST_PIPE_DIST ahead of the bird, but never already on screen."""
        return float(max(self.w + 2, self.bird_x + FIRST_PIPE_DIST))

    def gap_top_max(self, gap: int) -> int:
        return self.ground_y - GAP_MARGIN_BOTTOM - CAP_H - gap

    def resize(self, h: int, w: int) -> None:
        """New terminal size: re-layout and pause a human game so the resize is never a death."""
        self.h, self.w = h, w
        self.layout()
        if self.state == PLAYING and not self.demo and not self.paused and not self.too_small:
            self.paused, self.pause_reason = True, "RESIZED"

    # -- rounds ----------------------------------------------------------------
    def reset_round(self) -> None:
        speed, max_speed, gap, gap_min, spacing = DIFFICULTIES[DIFF_ORDER[self.diff]]
        self.base_speed, self.max_speed, self.gap0, self.gap_min, self.spacing = speed, max_speed, gap, gap_min, spacing
        self.speed, self.gap = speed, gap
        self.score, self.pipes, self.scroll = 0, [], 0.0
        self.spawn_x = self.first_spawn_x()
        self.bird = Bird(self.bird_x, self.hover_y, self.sprites)
        self.fx.clear()
        self.theme.night = False
        self.death_t = self.over_t = self.ready_t = 0.0
        self.new_best = self.paused = False

    def start_round(self) -> None:
        self.reset_round()
        self.state = READY

    def to_title(self) -> None:
        self.demo, self.state, self.title_t = False, TITLE, 0.0
        self.reset_round()

    def say(self, text: str) -> None:
        self.toast, self.toast_t = text, TOAST_TIME

    def take_over(self) -> None:
        """Hand a bot round to the player: the world keeps scrolling, but score and difficulty restart from zero
        (pipes already behind the bird stay scored, so they are never counted for the player)."""
        self.demo = False
        self.score = 0
        self.speed, self.gap = self.base_speed, self.gap0
        self.theme.night = False
        self.say("You have control!")

    def do_flap(self) -> None:
        self.bird.flap()
        self.fx.puff(self.bird.x - 0.5, self.bird.y + 1.0)

    def die(self) -> None:
        self.state, self.death_t = DYING, 0.0
        self.bird.tumble, self.bird.vy, self.bird.flap_anim = 1.0, min(self.bird.vy, -5.0), 0.0
        self.fx.shake, self.fx.flash = SHAKE_TIME, FLASH_TIME
        if self.sound:
            curses.flash()

    # -- input -----------------------------------------------------------------
    def handle_keys(self, keys: List[int]) -> None:
        """Apply a frame's worth of decoded keys; only the first flap counts."""
        flapped = False
        for ch in keys:
            if ch in QUIT_KEYS:
                self.running = False
                return
            if self.state == TITLE:
                self.title_t = 0.0
            if ch in (ord("m"), ord("M")):
                self.sound = not self.sound
                self.say("sound %s" % ("ON" if self.sound else "OFF"))
            elif self.too_small:
                continue
            elif ch in FLAP_KEYS:
                if not flapped:
                    flapped = True
                    self.on_flap_key()
            elif ch in (ord("p"), ord("P")) and self.state in (READY, PLAYING):
                self.paused, self.pause_reason = not self.paused, ""
            elif ch in (ord("t"), ord("T")) and (self.state == OVER or self.demo):
                self.to_title()
            elif self.state == TITLE and ch in (curses.KEY_LEFT, curses.KEY_RIGHT, ord("h"), ord("l")):
                self.diff = (self.diff + (1 if ch in (curses.KEY_RIGHT, ord("l")) else -1)) % len(DIFF_ORDER)
                self.reset_round()
            elif self.state == TITLE and ch in (ord("d"), ord("D")):
                self.demo = True
                self.start_round()
            elif self.state == OVER and ch in (ord("r"), ord("R")) and self.over_t > 0.3:
                self.start_round()

    def on_flap_key(self) -> None:
        if self.paused:
            self.paused = False
            if self.pause_reason == "RESIZED" and self.state == PLAYING:
                self.do_flap()
            self.pause_reason = ""
            return
        if self.state == DYING:
            return                                   # a bot's death tumble is never the player's run
        if self.demo and self.state in (READY, PLAYING):
            self.take_over()
        self.demo = False
        if self.state == TITLE:
            self.start_round()
        elif self.state == READY:
            self.state = PLAYING
            self.do_flap()
        elif self.state == PLAYING:
            self.do_flap()
        elif self.state == OVER and self.over_t > 0.3:
            self.start_round()

    # -- simulation ------------------------------------------------------------
    def step(self, dt: float) -> None:
        self.time += dt
        self.toast_t = max(0.0, self.toast_t - dt)
        if self.too_small or self.paused:
            return
        if self.state == TITLE:
            self.title_t += dt
            self.scroll += self.base_speed * 0.5 * dt
            self.bg.update(dt, self.base_speed * 0.5)
            self.bird.hover(dt, self.hover_y)
            if self.title_t >= ATTRACT_AFTER:
                self.demo = True
                self.start_round()
        elif self.state == READY:
            self.scroll += self.speed * dt
            self.bg.update(dt, self.speed)
            self.bird.hover(dt, self.hover_y)
            self.ready_t += dt
            if self.demo and self.ready_t > 0.8:
                self.state = PLAYING
                self.do_flap()
        elif self.state == PLAYING:
            self.step_play(dt)
        elif self.state == DYING:
            self.step_dying(dt)
        else:
            self.over_t += dt
            if self.demo and self.over_t > 2.5:
                self.start_round()
        self.fx.update(dt, self.speed if self.state == PLAYING else 0.0)

    def step_play(self, dt: float) -> None:
        if self.demo and Bot.wants_flap(self.bird, self.pipes, self.ground_y, self.speed):
            self.do_flap()
        self.scroll += self.speed * dt
        self.bg.update(dt, self.speed)
        for p in self.pipes:
            p.x -= self.speed * dt
        self.spawn_x -= self.speed * dt
        self.spawn_pipes()
        floor = float(self.ground_y - BIRD_H)
        self.bird.update(dt, floor)
        cells = self.bird.cells()
        if self.bird.y >= floor - 0.2 or any(p.hits(cells) for p in self.pipes):
            self.die()
            return
        for p in self.pipes:
            if not p.passed and p.x + PIPE_W <= self.bird.x:
                p.passed = True
                self.on_score()

    def on_score(self) -> None:
        self.score += 1
        self.speed = min(self.max_speed, self.base_speed + self.score * SPEED_PER_POINT)
        self.gap = max(self.gap_min, self.gap0 - self.score * GAP_SHRINK_PER_POINT)
        self.theme.night = (self.score // NIGHT_EVERY) % 2 == 1
        self.fx.popup(self.bird.x + BIRD_W + 1.0, self.bird.y - 0.5, "+1")
        if self.sound:
            curses.beep()

    def gap_shift_limits(self) -> Tuple[int, int]:
        """Fair (up, down) limits for the next gap's shift, from the free time between leaving one
        pipe and reaching the next: up is what chained flaps climb at a humane rate, down is what a
        bird drops after leaving the pipe low on a fresh hop - so every sequence stays reachable
        however fast the pipes get."""
        free_t = max(0.0, (self.spacing - CAP_W - BIRD_W) / self.speed)
        fall_t, t_term = max(0.0, free_t + FLAP_VY / GRAVITY), MAX_FALL_VY / GRAVITY
        drop = 0.5 * GRAVITY * min(fall_t, t_term) ** 2 + MAX_FALL_VY * max(0.0, fall_t - t_term)
        down = int(self.gap + 0.5) - BIRD_H - FLAP_RISE + 0.8 + drop
        return max(1, min(MAX_GAP_DELTA, int(CLIMB_RATE * free_t + 0.5))), max(1, min(MAX_GAP_DELTA, int(down)))

    def spawn_pipes(self) -> None:
        """Create each pipe as its column (spawn_x, scrolling with the world) reaches the right edge."""
        limit = self.w + 2
        self.pipes = [p for p in self.pipes if p.x + CAP_W + 2 > 0]
        while self.spawn_x <= limit:
            x, self.spawn_x = self.spawn_x, self.spawn_x + self.spacing
            gap = min(int(self.gap + 0.5), max(4, self.gap_top_max(GAP_TOP_MIN) - 2))
            lo, hi = GAP_TOP_MIN, max(GAP_TOP_MIN, self.gap_top_max(gap))
            if self.pipes:
                prev, (up, down) = self.pipes[-1].gap_top, self.gap_shift_limits()
                lo, hi = max(lo, prev - up), min(hi, prev + down)
                if lo > hi:
                    lo = hi = max(GAP_TOP_MIN, min(self.gap_top_max(gap), prev))
            top = self.rng.randint(lo, hi)
            self.pipes.append(Pipe(x, top, top + gap))

    def step_dying(self, dt: float) -> None:
        self.death_t += dt
        floor = float(self.ground_y - BIRD_H)
        self.bird.update(dt, floor)
        self.bird.wing_t += 6.0 * dt
        limit = DEATH_ANIM_MAX + self.ground_y / MAX_FALL_VY      # safety net scaled to the fall height
        if (self.bird.y >= floor - 0.01 and self.death_t > 0.5) or self.death_t > limit:
            self.state, self.over_t = OVER, 0.0
            self.new_best = not self.demo and self.store.record(DIFF_ORDER[self.diff], self.score)


# --------------------------------------------------------------------------- #
# Rendering
# --------------------------------------------------------------------------- #
class Renderer:
    """Draws the world, HUD and every screen from the game state."""

    def __init__(self, cv: Canvas, theme: Theme, glyphs: Glyphs) -> None:
        self.cv, self.th, self.g = cv, theme, glyphs
        self.big = BigText(glyphs)
        self.hint_row = 5            # row under the big score for the demo hint and toasts
        self._bkgd_night: Optional[bool] = None

    def frame(self, game: Game) -> None:
        cv, th = self.cv, self.th
        if th.painted and self._bkgd_night != th.night:   # erase() then paints the sky for free
            self._bkgd_night = th.night
            cv.scr.bkgd(" ", th.attr("sky"))
        cv.scr.erase()
        if game.too_small:
            self.too_small()
        elif game.fx.flash > 0:
            for y in range(cv.h):
                cv.put(y, 0, " " * cv.w, th.attr("flash"))
        else:
            cv.dx = (1 if int(game.time * 40) % 2 else -1) if game.fx.shake > 0 else 0
            self.world(game)
            cv.dx = 0
            if game.state == TITLE:
                self.title(game)
            else:
                self.hud(game)
                if game.state == READY and not game.paused:
                    self.ready(game)
                elif game.state == OVER:
                    self.game_over(game)
                if game.paused:
                    self.paused(game)
            if game.toast_t > 0:
                cv.center(0 if game.state == TITLE else self.hint_row, " %s " % game.toast, th.attr("accent"))
        cv.scr.noutrefresh()
        curses.doupdate()

    # -- world -----------------------------------------------------------------
    def world(self, game: Game) -> None:
        cv, th, g, bg = self.cv, self.th, self.g, game.bg
        gy = game.ground_y
        if th.night:
            a, band = th.attr("sun"), max(1, gy - bg.hill_rows - 4)
            for fx, row, ph in bg.stars:
                cv.put(1 + row * band // 10, int(fx * (cv.w - 1)), g.stars[int(game.time * 2 + ph) % 3], a)
        a = th.attr("cloud")
        for x, row, big in bg.clouds:
            for i, line in enumerate(g.cloud_big if big else g.cloud_small):
                cv.put_runs(row + i, int(x - bg.offsets[0]), line, a)
        a = th.attr("sun")
        for i, line in enumerate(g.moon if th.night else g.sun):
            cv.put_runs(1 + i, cv.w - 8, line, a)
        for lines, off, rows, role in ((bg.hill_lines, bg.offsets[1], bg.hill_rows, "hill_far"),
                                       (bg.bush_lines, bg.offsets[2], bg.bush_rows, "hill_near")):
            a = th.attr(role)
            for i, line in enumerate(bg.slice(lines, off, cv.w)):
                cv.put_runs(gy - rows + i, 0, line, a)
        for p in game.pipes:
            self.pipe(p, gy)
        for i, (pat, role) in enumerate(((g.grass, "grass"), (g.dirt1, "dirt"), (g.dirt2, "dirt"))):
            n, ph = len(pat), int(game.scroll) % len(pat)
            cv.put(gy + i, 0, (pat * (cv.w // n + 2))[ph:ph + cv.w], th.attr(role))
        pa, fa = th.attr("text_hi"), th.attr("cloud")
        for p in game.fx.items:
            ch = p.text or g.puff[min(2, int(3 * p.age / p.life))]
            cv.put(int(round(p.y)), int(round(p.x)), ch, pa if p.text else fa)
        if game.state != TITLE:
            self.bird(game.bird, game.bird.x, game.bird.row)

    def bird(self, bird: Bird, x: int, y: int) -> None:
        for dy, dx, ch, fg, bgr in bird.sprite():
            self.cv.put(y + dy, x + dx, ch, self.th.attr(fg, bgr or None))

    def pipe(self, p: Pipe, gy: int) -> None:
        cv, th, g = self.cv, self.th, self.g
        x0 = p.col
        if x0 - 1 >= cv.w or x0 + CAP_W <= 0:
            return
        hi, body, shade = th.attr("pipe_hi", "pipe"), th.attr("pipe"), th.attr("pipe_dark", "pipe")
        solid = th.painted or g.ascii           # the half-block highlight needs a painted background
        hi_ch, cap_hi = (g.pipe_hi, g.cap_hi) if solid else (g.full, g.full)
        body_mid = g.full * (PIPE_W - 1 - len(g.pipe_shade))
        cap_mid = g.cap_body * (CAP_W - 1 - len(g.cap_shade))
        for y in range(gy):
            if p.gap_top - CAP_H <= y < p.gap_top or p.gap_bot <= y < p.gap_bot + CAP_H:
                x, left, mid, right = x0 - 1, cap_hi, cap_mid, g.cap_shade
            elif y < p.gap_top or y >= p.gap_bot:
                x, left, mid, right = x0, hi_ch, body_mid, g.pipe_shade
            else:
                continue
            cv.put(y, x, left, hi)
            cv.put(y, x + 1, mid, body)
            cv.put(y, x + 1 + len(mid), right, shade)

    # -- overlays --------------------------------------------------------------
    def hud(self, game: Game) -> None:
        cv, th = self.cv, self.th
        half = cv.w < 80 or game.ground_y < 22          # wide screens get 5-row, double-width digits
        self.hint_row = 1 + self.big.height(half) + 1
        if game.state != OVER:
            self.big.draw(cv, 1, str(game.score), th.attr("text_hi"), half=half, backing=th.attr("sky"), pad=1,
                          xscale=1 if half else 2)                   # the halo keeps a passing pipe off the digits
        if game.demo:
            cv.put(0, 0, " DEMO  score %d " % game.score, th.attr("text_hi"))
            if game.toast_t <= 0:
                cv.center(self.hint_row, " any flap key takes over ", th.attr("text"))
        else:
            cv.put(0, 0, " %s " % DIFF_ORDER[game.diff], th.attr("text"))
        best = " BEST %d " % game.best
        cv.put(0, cv.w - len(best), best, th.attr("text"))

    def _banner(self, game: Game, y: int, text: str, hint: str) -> None:
        """Block letters at row y with the hint under them, both kept above the skyline; when the blocks do not
        fit there (5-row ASCII glyphs on a short screen) a single text line at y says the same."""
        cv, th, big = self.cv, self.th, self.big
        room = game.ground_y - max(game.bg.hill_rows, game.bg.bush_rows)     # rows above the skyline band
        h = big.height(True)
        gap = 1 if y + h + 2 <= room else 0                                  # the blank line under the blocks
        if y + h + gap < room:
            big.draw(cv, y, text, th.attr("accent"), backing=th.attr("sky"), pad=1)
            cv.center(y + h + gap, " %s " % hint, th.attr("text_hi"))
        else:
            line = "%s - %s" % (text, hint)
            cv.center(min(y, room - 1), " %s " % (line if len(line) + 2 <= cv.w else hint), th.attr("text_hi"))

    def ready(self, game: Game) -> None:
        hint = "autopilot engaged" if game.demo else "SPACE / UP / W / K  to flap"
        below = int(game.hover_y + HOVER_BOB + 0.5) + BIRD_H         # first row the bobbing bird never covers
        self._banner(game, below, "GET READY", hint)
        if not game.demo and int(game.time * 3) % 2:
            self.cv.put(game.bird.row, game.bird.x + BIRD_W + 1, "flap!", self.th.attr("text_hi"))

    def paused(self, game: Game) -> None:
        msg = "press P or SPACE to resume"
        if game.pause_reason == "RESIZED":
            msg = "window resized - " + msg                      # 44 columns: fits every allowed width
        self._banner(game, max(self.big.height(True) + 2, game.ground_y // 3), "PAUSED", msg)

    def title(self, game: Game) -> None:
        cv, th, g, big, bg = self.cv, self.th, self.g, self.big, game.bg
        w, gy = cv.w, game.ground_y
        logo, sky = th.attr("bird"), th.attr("sky")
        one_line = w >= big.width("FLAPPY BIRD") + BIRD_W + 8        # room for the bird beside the logo
        tall = one_line and gy >= 22 and not g.ascii                 # full-height logo with a drop shadow
        blink = "accent" if int(game.time * 2) % 2 else "text_hi"
        lines = [("BEST  %d" % game.best, "text_hi"),
                 ("%s  %s  %s   Left / Right" % (g.arrows[0], DIFF_ORDER[game.diff], g.arrows[1]), "text_hi"),
                 ("", ""), ("PRESS SPACE TO START", blink), ("", ""),
                 ("SPACE UP W K  flap    P  pause    M  sound %s" % ("ON" if game.sound else "OFF"), "text"),
                 ("D  demo    Q  quit", "text")]
        logo_h = (6 if tall else big.height(True)) if one_line else 2 * big.height(True)
        room = gy - max(bg.hill_rows, bg.bush_rows)                  # rows above the skyline band
        if logo_h + 1 + len(lines) > room:
            lines = [ln for ln in lines if ln[0]]
        compact = not one_line and logo_h + 1 + len(lines) > room    # short ASCII screen: BIRD as plain text
        if compact:
            logo_h = big.height(True) + 1
        if logo_h + 1 + len(lines) > room:
            lines = [ln for ln in lines if ln[1] != "text"]          # last resort: drop the key help
        text = "FLAPPY BIRD" if one_line else "FLAPPY"
        lx = (w - big.width(text)) // 2
        y = max(0, (room - logo_h - 1 - len(lines)) // 3)
        if lx + big.width(text) > w - 10:                            # keep clear of the sun
            y = max(y, min(4, room - logo_h - 1 - len(lines)))
        big.draw(cv, y, text, logo, lx, th.attr("logo_sh") if tall else None, half=not tall, backing=sky)
        if compact:
            cv.center(y + logo_h - 1, " B I R D ", logo)
        elif not one_line:
            big.draw(cv, y + big.height(True), "BIRD", logo, backing=sky)
        bob = int(round(game.bird.y - game.hover_y))
        self.bird(game.bird, max(0, lx - BIRD_W - 2), y + (logo_h - BIRD_H) // 2 + bob)
        y += logo_h + 1
        for text, role in lines:
            if text:
                cv.center(y, "  %s  " % text, th.attr(role))
            y += 1

    def game_over(self, game: Game) -> None:
        cv, th, g, big = self.cv, self.th, self.g, self.big
        gy = game.ground_y
        pw, ph = min(46, cv.w - 4), 9
        total = big.height(True) + 1 + ph
        y = max(1, (gy - total) // 2)
        if total <= gy - 1 and big.width("GAME OVER") + 2 <= cv.w - 10:    # keep clear of the sun, like title()
            y += big.draw(cv, y, "GAME OVER", th.attr("accent"), backing=th.attr("sky"), pad=1) + 1
        else:
            y = max(0, (gy - ph) // 2)
        y = max(0, min(y, gy - BIRD_H - ph))                                # the panel never covers the dead bird
        x = (cv.w - pw) // 2
        pa, dim, b = th.attr("panel"), th.attr("panel_dim"), g.box
        for i in range(ph):
            left, mid, right = (b[0], b[1], b[2]) if i == 0 else (b[4], b[1], b[5]) if i == ph - 1 else (b[3], " ", b[3])
            cv.put(y + i, x, left + mid * (pw - 2) + right, pa)
        cv.put(y, x + (pw - 11) // 2, " GAME OVER ", pa)
        medal = medal_for(game.score)
        if medal:
            for i, line in enumerate(g.medal):
                cv.put_runs(y + 2 + i, x + 3, line, th.attr(medal))
            cv.put(y + 3, x + 5, medal[0].upper(), th.attr("panel", medal))
            cv.put(y + 5, x + 3 + (5 - len(medal)) // 2, medal.upper(), dim)
        else:
            cv.put(y + 3, x + 3, "no medal", dim)
        for i, (name, val) in enumerate((("SCORE", game.score), ("BEST", game.best))):
            cv.put(y + 2 + i, x + 14, name, dim)
            cv.put(y + 2 + i, x + pw - 3 - len(str(val)), str(val), pa)
        if game.new_best:
            cv.put(y + 4, x + 14, "NEW BEST!", th.attr("panel_hi"))
        ctl = "DEMO  restarting..." if game.demo else "SPACE / R  restart   T  title   Q  quit"
        cv.put(y + ph - 2, x + (pw - len(ctl)) // 2, ctl, pa)

    def too_small(self) -> None:
        cv = self.cv
        msgs = ("Terminal too small", "resize to at least %dx%d (now %dx%d)" % (MIN_COLS, MIN_ROWS, cv.w, cv.h),
                "(q quits)")
        for i, m in enumerate(msgs):
            cv.put(max(0, cv.h // 2 - 1) + i, max(0, (cv.w - len(m)) // 2), m, curses.A_BOLD if i == 0 else 0)


# --------------------------------------------------------------------------- #
# Terminal plumbing: input decoding, size safety net, locale
# --------------------------------------------------------------------------- #
class KeyReader:
    """Reads every queued key without blocking and decodes what terminfo did not: raw ESC [ X / ESC O X
    arrow sequences become curses keys, any other CSI sequence (ESC [ 1 ; 2 A, bracketed paste ...) is
    swallowed, and a lone ESC stays ESC.  A sequence cut off by the end of a frame (Alt+[, an arrow on a
    laggy link) is held for up to HOLD seconds to be completed by the next bytes, then dropped."""

    HOLD = 0.15

    def __init__(self) -> None:
        self.tail: List[int] = []
        self.tail_t = 0.0

    def drain(self, scr: "curses.window", now: Optional[float] = None) -> List[int]:
        now = time.perf_counter() if now is None else now
        held = self.tail if now - self.tail_t <= self.HOLD else []
        raw, self.tail = list(held), []
        while True:
            try:
                ch = scr.getch()
            except curses.error:
                break
            if ch == -1:
                break
            raw.append(ch)
        keys, i = [], 0
        while i < len(raw):
            ch, i = raw[i], i + 1
            if ch == 27 and i < len(raw) and raw[i] in (ord("["), ord("O")):
                j = i + 1
                while j < len(raw) and j < i + 9 and not 0x40 <= raw[j] <= 0x7E:
                    j += 1
                if j == len(raw) and j < i + 9:             # no final byte yet: finish it next frame
                    self.tail = raw[i - 1:]
                    if self.tail != held:                    # a new tail starts the hold; an old one keeps waiting
                        self.tail_t = now
                    break
                ch, i = (ARROW_KEYS.get(raw[j], -2) if j == i + 1 else -2), j + 1   # parameters -> not an arrow
            keys.append(ch)
        return keys


def sync_terminal_size(scr: "curses.window", resized: bool = False) -> None:
    """Safety net for a missed SIGWINCH: make curses match the size the OS reports.  PDCurses on Windows only
    learns the new console size through resize_term(0, 0), called on KEY_RESIZE or when the sizes disagree."""
    try:
        size = os.get_terminal_size(sys.__stdout__.fileno())
    except (OSError, ValueError, AttributeError):
        size = None
    if WINDOWS:
        if resized or (size is not None and (size.lines, size.columns) != scr.getmaxyx()):
            try:
                curses.resize_term(0, 0)
            except (curses.error, AttributeError):
                pass
        return
    if size is None:
        return
    if size.lines > 0 and size.columns > 0 and (size.lines, size.columns) != scr.getmaxyx() \
            and hasattr(curses, "resizeterm"):
        try:
            curses.resizeterm(size.lines, size.columns)
        except curses.error:
            pass


def terminal_is_utf8() -> bool:
    """True when curses will encode output as UTF-8.  Uses nl_langinfo(CODESET), which is what curses
    itself encodes with; getpreferredencoding() reports utf-8 under Python's UTF-8 mode even for LC_ALL=C."""
    if WINDOWS:                          # windows-curses writes through the wide-character console API
        return True
    try:
        locale.setlocale(locale.LC_ALL, "")
    except locale.Error:
        pass
    enc = ""
    if hasattr(locale, "nl_langinfo") and hasattr(locale, "CODESET"):
        try:
            enc = locale.nl_langinfo(locale.CODESET)
        except (ValueError, AttributeError):
            enc = ""
    try:
        return codecs.lookup(enc or locale.getpreferredencoding(False) or "ascii").name == "utf-8"
    except LookupError:
        return False


# --------------------------------------------------------------------------- #
# Main loop, self-test and entry point
# --------------------------------------------------------------------------- #
def run(scr: "curses.window", opts: argparse.Namespace) -> None:
    """curses.wrapper target: fixed 60 Hz physics, paced rendering, no busy-wait."""
    try:
        curses.curs_set(0)
    except curses.error:
        pass
    scr.nodelay(True)
    scr.keypad(True)
    if hasattr(curses, "set_escdelay"):
        try:
            curses.set_escdelay(25)
        except curses.error:
            pass
    theme, glyphs, cv = Theme(not opts.no_color), Glyphs(opts.ascii), Canvas(scr)
    game = Game(cv.h, cv.w, theme, glyphs, opts, ScoreStore())
    renderer = Renderer(cv, theme, glyphs)
    frame_dt = 1.0 / opts.fps
    max_steps = max(MAX_STEPS_PER_FRAME, math.ceil(PHYSICS_HZ / opts.fps) + 2)   # a 10 fps frame needs 6 + jitter
    last = next_frame = size_check = time.perf_counter()
    acc, keys = 0.0, KeyReader()
    while game.running:
        pressed = keys.drain(scr)
        game.handle_keys(pressed)
        now = time.perf_counter()
        resized = curses.KEY_RESIZE in pressed
        if resized or now - size_check > 0.5:
            size_check = now
            sync_terminal_size(scr, resized)
        if cv.refresh_size():
            game.resize(cv.h, cv.w)
        acc += min(now - last, 0.25)
        last = now
        steps = 0
        while acc >= PHYSICS_DT and steps < max_steps:
            game.step(PHYSICS_DT)
            acc -= PHYSICS_DT
            steps += 1
        if acc >= PHYSICS_DT:            # still behind after the cap: drop the stalled time, do not spiral
            acc = 0.0
        renderer.frame(game)
        next_frame += frame_dt
        delay = next_frame - time.perf_counter()
        if delay > 0:
            time.sleep(delay)
        else:
            next_frame = time.perf_counter()


def selftest() -> int:
    """Headless checks of the model, the bot and the helpers; prints a report, returns the failure count."""
    import tempfile
    fails: List[str] = []
    tmp = tempfile.mkdtemp()

    def check(cond: bool, msg: str) -> None:
        print(("ok    " if cond else "FAIL  ") + msg)
        if not cond:
            fails.append(msg)

    def new_game(w: int = 100, h: int = 30, diff: str = "NORMAL", seed: int = 1, demo: bool = True) -> Game:
        opts = argparse.Namespace(seed=seed, demo=demo, difficulty=diff)
        return Game(h, w, Theme(False), Glyphs(True), opts, ScoreStore(os.path.join(tmp, "unused.json")))

    for diff in DIFF_ORDER:                      # 1. the autopilot survives and scores on every preset and size
        for w, h in ((100, 30), (80, 24), (50, 16), (160, 50)):
            for seed in (1, 7, 11):
                game, died = new_game(w, h, diff, seed), False
                for _ in range(60 * PHYSICS_HZ):
                    game.step(PHYSICS_DT)
                    died = died or game.state in (DYING, OVER)
                want = int(50 * game.base_speed / game.spacing * 0.7)
                check(not died and game.score >= want, "%s %dx%d seed %d: bot survives 60 s, score %d (want >= %d)"
                      % (diff, w, h, seed, game.score, want))
    game = new_game(demo=False)                  # 2. collision geometry
    game.state, bird, pipe = PLAYING, game.bird, Pipe(0.0, 10, 17)

    def hit_at(y: float, px: float) -> bool:
        bird.y, pipe.x = y, px
        return pipe.hits(bird.cells())

    bx = float(game.bird_x)
    check([hit_at(12, bx), hit_at(8, bx), hit_at(16, bx), hit_at(8, bx + BIRD_W + 1)] == [False, True, True, False],
          "the gap is safe, both caps kill, a cap one column ahead of the beak does not")
    bird.y, bird.vy = 0.2, -40.0
    bird.update(PHYSICS_DT, 20.0)
    check(bird.y == 0.0 and bird.vy == 0.0, "ceiling clamps without death")
    game.pipes, bird.y = [], float(game.ground_y - BIRD_H)
    game.step(PHYSICS_DT)
    check(game.state == DYING, "ground kills")
    game = new_game(demo=False)                  # 3. scoring once, ramp limits, fairness, reset
    game.state, game.pipes, game.bird.y = PLAYING, [Pipe(game.bird_x - PIPE_W + 0.5, 5, 25)], 10.0
    for _ in range(12):
        game.step(PHYSICS_DT)
    check(game.score == 1 and game.state == PLAYING, "a passed pipe scores exactly once (score %d)" % game.score)
    first = game.gap_shift_limits()
    for _ in range(80):
        game.on_score()
    last = game.gap_shift_limits()
    check(game.speed == game.max_speed and game.gap == game.gap_min, "speed and gap ramps stop at their limits")
    check(all(1 <= v <= MAX_GAP_DELTA for v in first + last) and last <= first,
          "fair gap shifts stay within [1, %d] and tighten with speed (%s -> %s)" % (MAX_GAP_DELTA, first, last))
    game.start_round()
    check(game.score == 0 and not game.pipes and game.speed == game.base_speed and game.gap == game.gap0
          and game.state == READY, "restart resets score, pipes, speed and gap")
    game = new_game(120, 40)                     # 4. resizing keeps gaps on screen and the bird off the ground
    for _ in range(10 * PHYSICS_HZ):
        game.step(PHYSICS_DT)
    game.bird.y = 35.0
    game.resize(16, 50)
    check(game.state == PLAYING and not game.too_small and game.bird.y <= game.ground_y - BIRD_H - 3 and all(
        GAP_TOP_MIN <= p.gap_top < p.gap_bot <= game.ground_y - 1 for p in game.pipes),
        "resize clamps every gap and lifts a low bird clear of the ground")
    gaps = [(p.gap_top, p.gap_bot) for p in game.pipes]
    game.resize(10, 40)
    game.step(PHYSICS_DT)
    game.resize(30, 100)
    check(not game.too_small and game.state == PLAYING and gaps == [(p.gap_top, p.gap_bot) for p in game.pipes],
          "too-small screen freezes the world and leaves the pipes intact")
    game = new_game(80, 24)
    for _ in range(6 * PHYSICS_HZ):
        game.step(PHYSICS_DT)
    score, ahead = game.score, [p.x - game.bird.x for p in game.pipes]
    drift, safe = 0.0, True
    for w in (160, 50, 80):
        game.resize(24, w)
        drift = max([drift] + [abs(p.x - game.bird.x - d) for p, d in zip(game.pipes, ahead)])
        safe = safe and not any(p.hits(game.bird.cells()) for p in game.pipes)
    game.step(PHYSICS_DT)
    check(game.state == PLAYING and game.score == score and drift < 1e-6 and safe,
          "a width change moves the pipes with the bird: no free points, no pipe through the bird")
    for w in (80, 160, 240):                     # the first pipes scroll in from the edge whatever the width
        game = new_game(w, 40, demo=False)
        game.on_flap_key()
        game.on_flap_key()
        game.step(PHYSICS_DT)
        check(all(p.x >= w for p in game.pipes), "%d columns: no pipe pops up on screen at the first tick" % w)
    calls: List[Tuple[int, int, str]] = []       # 5. the canvas never raises, whatever you draw where

    def addstr(y: int, x: int, s: str, attr: int = 0) -> None:   # raises like ncurses, encodes like a C locale
        if not (0 <= y < 10 and 0 <= x < 20 and x + len(s) <= 20):
            raise curses.error("addstr() out of bounds")
        s.encode("ascii")
        calls.append((y, x, s))
        if y == 9 and x + len(s) == 20:
            raise curses.error("bottom-right cell")

    cv = Canvas(argparse.Namespace(getmaxyx=lambda: (10, 20), addstr=addstr))   # type: ignore[arg-type]
    for args in ((-1, 0, "x"), (9, 19, "!"), (9, 15, "abcdefgh"), (3, -2, "abc"), (99, 99, "z"), (0, 25, "q"),
                 (2, 2, ""), (4, 4, "██")):
        cv.put(*args)
    check(calls == [(9, 19, "!"), (9, 15, "abcde"), (3, 0, "c"), (4, 4, "??")],
          "canvas clips, survives the bottom-right cell and degrades non-ASCII")
    raw = iter([27, 91, 65, 32, 27, 79, 68, 27, 91, 49, 59, 50, 65, 27, 91, 50, 48, 48, 126, 27, 113, -1])
    reader = KeyReader()
    check(reader.drain(argparse.Namespace(getch=lambda: next(raw)), now=0.0)   # type: ignore[arg-type]
          == [curses.KEY_UP, 32, curses.KEY_LEFT, -2, -2, 27, 113], "raw arrow / CSI / lone ESC decoding")
    split = iter([32, 27, 91, -1, 65, -1, 27, 79, -1, -1, 113, -1, 27, 91, 49, 59, -1, 50, 65, 113, -1])
    scr = argparse.Namespace(getch=lambda: next(split))
    check([reader.drain(scr, now=t) for t in (1.0, 1.1, 1.2, 1.3, 1.4, 3.0, 3.1)]   # type: ignore[arg-type]
          == [[32], [curses.KEY_UP], [], [], [113], [], [-2, 113]],
          "ESC [ / ESC O cut off by a frame: finished next frame, dropped after the hold (q still quits), no crash")
    path = os.path.join(tmp, "scores.json")      # 6. score files: corrupt, legacy, missing, round-trip
    with open(path, "w") as fh:
        fh.write("{not json")
    store = ScoreStore(path)
    check(store.get("NORMAL") == 0 and store.record("NORMAL", 7) and ScoreStore(path).get("NORMAL") == 7
          and not store.record("NORMAL", 3), "corrupt score file tolerated, new best persisted once")
    with open(path, "w") as fh:
        fh.write('{"best": 12}')
    check(ScoreStore(path).get("NORMAL") == 12 and ScoreStore(path).get("HARD") == 0, "legacy score file read")
    check(ScoreStore(os.path.join(tmp, "missing", "x.json")).get("EASY") == 0, "missing score file tolerated")
    with open(path, "w") as fh:
        fh.write('{"best": {"NORMAL": 1e400, "EASY": NaN, "HARD": 5, "X": 1}}')
    check(ScoreStore(path).best == {"HARD": 5}, "infinite / NaN / unknown entries in the score file are skipped")
    game = new_game(demo=True, seed=7)           # 7. the autopilot's crashes are not the player's best
    game.store = ScoreStore(path)
    for _ in range(6 * PHYSICS_HZ):
        game.step(PHYSICS_DT)
    game.pipes.append(Pipe(float(game.bird.x), GAP_TOP_MIN, GAP_TOP_MIN + 4))   # a pipe through the bird
    for _ in range(3 * PHYSICS_HZ):
        game.step(PHYSICS_DT)
    check(game.state == OVER and game.demo and not game.new_best and ScoreStore(path).get("NORMAL") == 0,
          "a demo death records no best (score %d, state %d)" % (game.score, game.state))
    check(medal_for(9) is None and medal_for(10) == "bronze" and medal_for(45) == "platinum", "medal thresholds")
    print("selftest: %d failure(s)" % len(fails))
    return len(fails)


def parse_args(argv: Optional[List[str]]) -> argparse.Namespace:
    ap = argparse.ArgumentParser(description="Flappy Bird in the terminal.",
                                 epilog="Keys: Space/Up/w/k flap, p pause, m sound, d demo, t title, q/Esc quit, "
                                        "Left/Right difficulty on the title screen.")
    ap.add_argument("--ascii", action="store_true", help="plain ASCII graphics (automatic when the locale is not UTF-8)")
    ap.add_argument("--no-color", action="store_true", help="monochrome rendering")
    ap.add_argument("--fps", type=int, default=60, help="render frame rate (default 60; physics is always 60 Hz)")
    ap.add_argument("--seed", type=int, default=None, help="deterministic pipe sequence")
    level = ap.add_mutually_exclusive_group()
    level.add_argument("--easy", action="store_true", help="wider gaps, slower pipes")
    level.add_argument("--hard", action="store_true", help="narrower gaps, faster pipes")
    ap.add_argument("--demo", action="store_true", help="attract mode: the game plays itself")
    ap.add_argument("--selftest", action="store_true", help="run the headless model checks and exit")
    opts = ap.parse_args(argv)
    opts.difficulty = "EASY" if opts.easy else "HARD" if opts.hard else "NORMAL"
    opts.fps = max(10, min(240, opts.fps))
    return opts


def main(argv: Optional[List[str]] = None) -> int:
    os.environ.setdefault("ESCDELAY", "25")
    opts = parse_args(argv)
    if opts.selftest:
        return 1 if selftest() else 0
    opts.ascii = opts.ascii or not terminal_is_utf8()
    try:
        curses.wrapper(run, opts)
    except KeyboardInterrupt:
        pass
    except curses.error as exc:          # no terminfo entry for $TERM, or no terminal at all
        print("flappy: cannot initialise the terminal (%s); check $TERM" % exc, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
