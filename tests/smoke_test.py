#!/usr/bin/env python3
"""
smoke_test.py - pty-based smoke tester for terminal (curses) games.

Runs a game inside a pseudo-terminal, feeds it keystrokes on a schedule,
resizes the window, measures CPU usage, and renders the game's escape-sequence
output through a small VT100/xterm emulator so you can SEE what a player sees.

Usage:
    python3 smoke_test.py path/to/game.py              # run all scenarios
    python3 smoke_test.py path/to/game.py --show       # also print screen snapshots
    python3 smoke_test.py path/to/game.py --only play  # run one scenario
    python3 smoke_test.py path/to/game.py --list       # list scenarios
    python3 smoke_test.py path/to/game.py --python /usr/bin/python3

Importable API (write your own scripted sessions):
    from smoke_test import run_game, Screen
    res = run_game("flappy.py", cols=100, rows=30, steps=[
        {"wait": 1.0, "snap": "title"},            # wait, then snapshot the screen
        {"wait": 0.2, "keys": b" "},               # wait, then send keys
        {"wait": 3.0, "snap": "playing", "resize": (40, 120)},  # (rows, cols)
        {"wait": 0.5, "keys": b"q"},
    ], extra_args=["--seed", "1"])
    res.returncode      # exit code (None if killed after timeout)
    res.snaps["title"]  # rendered screen text at that point
    res.final_screen    # rendered screen when the process ended
    res.text            # raw output (escape sequences included)
    res.cpu_ratio       # child CPU seconds / wall seconds
    res.has_traceback() / res.restored_terminal()

Step keys: wait (seconds, before anything else in the step), snap (label),
resize ((rows, cols)), keys (bytes). Order inside a step: wait -> snap -> resize -> keys.
"""
from __future__ import annotations

import argparse
import codecs
import fcntl
import os
import pty
import re
import resource
import select
import signal
import struct
import subprocess
import sys
import tempfile
import termios
import time
import unicodedata

# --------------------------------------------------------------------------- #
# Minimal VT100 / xterm emulator (enough for ncurses output)
# --------------------------------------------------------------------------- #

ACS = {  # DEC special graphics (ESC ( 0) -> Unicode
    "j": "┘", "k": "┐", "l": "┌", "m": "└", "n": "┼", "q": "─", "t": "├", "u": "┤",
    "v": "┴", "w": "┬", "x": "│", "a": "▒", "`": "◆", "f": "°", "g": "±", "~": "·",
    "o": "⎺", "p": "⎻", "r": "⎼", "s": "⎽", "y": "≤", "z": "≥", "{": "π", "|": "≠",
    "}": "£", "0": "█", "_": " ",
}


def _clamp(v: int, lo: int, hi: int) -> int:
    return lo if v < lo else hi if v > hi else v


class Screen:
    """Character-cell screen fed with terminal output; renders to plain text."""

    def __init__(self, rows: int, cols: int) -> None:
        self.rows, self.cols = rows, cols
        self.buf = [[" "] * cols for _ in range(rows)]
        self._reset_state()

    def _reset_state(self) -> None:
        self.r = self.c = 0
        self.top, self.bot = 0, self.rows - 1
        self.g = ["B", "0"]
        self.shift = 0
        self.saved = (0, 0)
        self.wrap = False
        self.last = " "
        self.state = "norm"
        self.seq = ""

    def resize(self, rows: int, cols: int) -> None:
        new = [[" "] * cols for _ in range(rows)]
        for y in range(min(rows, self.rows)):
            for x in range(min(cols, self.cols)):
                new[y][x] = self.buf[y][x]
        self.buf, self.rows, self.cols = new, rows, cols
        self.top, self.bot = 0, rows - 1
        self.r, self.c = _clamp(self.r, 0, rows - 1), _clamp(self.c, 0, cols - 1)
        self.wrap = False

    # -- output -------------------------------------------------------------
    def text(self, strip: bool = True) -> str:
        lines = ["".join(row) for row in self.buf]
        return "\n".join(line.rstrip() if strip else line for line in lines)

    def framed(self) -> str:
        bar = "+" + "-" * self.cols + "+"
        body = []
        for row in self.buf:
            s = "".join(row)
            # wide chars occupy 2 cells but 1 string char; pad by cell count
            cells = sum(0 if ch == "" else (2 if _wide(ch[:1]) else 1) for ch in row)
            body.append("|" + s + " " * max(0, self.cols - cells) + "|")
        return "\n".join([bar, *body, bar])

    # -- input --------------------------------------------------------------
    def feed(self, data: str) -> None:
        for ch in data:
            st = self.state
            if st == "norm":
                if ch == "\x1b":
                    self.state, self.seq = "esc", ""
                elif ch == "\r":
                    self.c, self.wrap = 0, False
                elif ch in "\n\x0b\x0c":
                    self._lf()
                elif ch == "\b":
                    self.c, self.wrap = max(0, self.c - 1), False
                elif ch == "\t":
                    self.c, self.wrap = min(self.cols - 1, (self.c // 8 + 1) * 8), False
                elif ch == "\x0e":
                    self.shift = 1
                elif ch == "\x0f":
                    self.shift = 0
                elif ch < " " or ch == "\x7f":
                    pass
                else:
                    self._put(ch)
            elif st == "esc":
                if ch == "[":
                    self.state, self.seq = "csi", ""
                elif ch == "]":
                    self.state = "osc"
                elif ch in "()*+":
                    self.state, self.seq = "cs", ch
                elif ch in "#%":
                    self.state = "skip1"
                elif ch == "7":
                    self.saved, self.state = (self.r, self.c), "norm"
                elif ch == "8":
                    (self.r, self.c), self.wrap, self.state = self.saved, False, "norm"
                elif ch == "D":
                    self._lf(); self.state = "norm"
                elif ch == "M":
                    self._ri(); self.state = "norm"
                elif ch == "E":
                    self.c = 0; self._lf(); self.state = "norm"
                elif ch == "c":
                    self.buf = [[" "] * self.cols for _ in range(self.rows)]
                    self._reset_state()
                elif ch in "P_^X":
                    self.state = "str"
                else:
                    self.state = "norm"
            elif st == "cs":
                if self.seq == "(":
                    self.g[0] = ch
                elif self.seq == ")":
                    self.g[1] = ch
                self.state = "norm"
            elif st == "skip1":
                self.state = "norm"
            elif st == "osc":
                if ch == "\x07":
                    self.state = "norm"
                elif ch == "\x1b":
                    self.state = "osc_esc"
            elif st == "osc_esc":
                self.state = "norm" if ch == "\\" else "osc"
            elif st == "str":
                if ch == "\x1b":
                    self.state = "str_esc"
            elif st == "str_esc":
                self.state = "norm" if ch == "\\" else "str"
            elif st == "csi":
                if "\x40" <= ch <= "\x7e":
                    self._csi(self.seq, ch)
                    self.state = "norm"
                elif ch == "\x1b":
                    self.state, self.seq = "esc", ""
                else:
                    self.seq += ch

    def _put(self, ch: str) -> None:
        if self.g[self.shift] == "0":
            ch = ACS.get(ch, ch)
        if unicodedata.combining(ch):
            if self.c > 0:
                self.buf[self.r][self.c - 1] += ch
            return
        w = 2 if _wide(ch) else 1
        if self.wrap:
            self.c, self.wrap = 0, False
            self._lf()
        if self.c + w > self.cols:
            self.c = 0
            self._lf()
        self.buf[self.r][self.c] = ch
        if w == 2 and self.c + 1 < self.cols:
            self.buf[self.r][self.c + 1] = ""
        self.last = ch
        if self.c + w >= self.cols:
            self.c, self.wrap = self.cols - 1, True
        else:
            self.c += w

    def _lf(self) -> None:
        if self.r == self.bot:
            self._scroll_up(1)
        elif self.r < self.rows - 1:
            self.r += 1

    def _ri(self) -> None:
        if self.r == self.top:
            self._scroll_down(1)
        elif self.r > 0:
            self.r -= 1

    def _scroll_up(self, n: int) -> None:
        for _ in range(n):
            del self.buf[self.top]
            self.buf.insert(self.bot, [" "] * self.cols)

    def _scroll_down(self, n: int) -> None:
        for _ in range(n):
            del self.buf[self.bot]
            self.buf.insert(self.top, [" "] * self.cols)

    def _csi(self, seq: str, f: str) -> None:
        priv = seq[:1] in ("?", ">", "=", "!") and seq != ""
        body = seq[1:] if priv else seq
        body = "".join(ch for ch in body if ch.isdigit() or ch in ";:")
        parts = body.replace(":", ";").split(";") if body else []
        P = [int(p) if p.isdigit() else 0 for p in parts]

        def p(i: int, d: int = 1) -> int:
            v = P[i] if i < len(P) else 0
            return v if v else d

        if priv:
            return  # DECSET/DECRST (alt screen, cursor visibility, ...) are irrelevant to the cell grid
        rows, cols = self.rows, self.cols
        row = self.buf[self.r]
        if f in "Hf":
            self.r, self.c = _clamp(p(0) - 1, 0, rows - 1), _clamp(p(1) - 1, 0, cols - 1)
        elif f == "A":
            self.r = max(0, self.r - p(0))
        elif f == "B":
            self.r = min(rows - 1, self.r + p(0))
        elif f == "C":
            self.c = min(cols - 1, self.c + p(0))
        elif f == "D":
            self.c = max(0, self.c - p(0))
        elif f == "E":
            self.r, self.c = min(rows - 1, self.r + p(0)), 0
        elif f == "F":
            self.r, self.c = max(0, self.r - p(0)), 0
        elif f in "G`":
            self.c = _clamp(p(0) - 1, 0, cols - 1)
        elif f == "d":
            self.r = _clamp(p(0) - 1, 0, rows - 1)
        elif f == "J":
            mode = P[0] if P else 0
            if mode == 0:
                row[self.c:] = [" "] * (cols - self.c)
                for y in range(self.r + 1, rows):
                    self.buf[y] = [" "] * cols
            elif mode == 1:
                row[: self.c + 1] = [" "] * (self.c + 1)
                for y in range(0, self.r):
                    self.buf[y] = [" "] * cols
            else:
                self.buf = [[" "] * cols for _ in range(rows)]
        elif f == "K":
            mode = P[0] if P else 0
            if mode == 0:
                row[self.c:] = [" "] * (cols - self.c)
            elif mode == 1:
                row[: self.c + 1] = [" "] * (self.c + 1)
            else:
                self.buf[self.r] = [" "] * cols
        elif f == "L":
            if self.top <= self.r <= self.bot:
                for _ in range(min(p(0), self.bot - self.r + 1)):
                    del self.buf[self.bot]
                    self.buf.insert(self.r, [" "] * cols)
        elif f == "M":
            if self.top <= self.r <= self.bot:
                for _ in range(min(p(0), self.bot - self.r + 1)):
                    del self.buf[self.r]
                    self.buf.insert(self.bot, [" "] * cols)
        elif f == "P":
            n = min(p(0), cols - self.c)
            del row[self.c : self.c + n]
            row.extend([" "] * n)
        elif f == "@":
            n = min(p(0), cols - self.c)
            row[self.c : self.c] = [" "] * n
            del row[cols:]
        elif f == "X":
            for x in range(self.c, min(cols, self.c + p(0))):
                row[x] = " "
        elif f == "S":
            self._scroll_up(p(0))
        elif f == "T":
            self._scroll_down(p(0))
        elif f == "b":
            for _ in range(p(0)):
                self._put(self.last)
            return
        elif f == "r":
            top, bot = p(0) - 1, p(1, rows) - 1
            top, bot = _clamp(top, 0, rows - 1), _clamp(bot, 0, rows - 1)
            if top < bot:
                self.top, self.bot = top, bot
            self.r = self.c = 0
        elif f == "s":
            self.saved = (self.r, self.c)
        elif f == "u":
            self.r, self.c = self.saved
        elif f == "Z":
            self.c = max(0, ((self.c - 1) // 8) * 8)
        elif f == "I":
            self.c = min(cols - 1, (self.c // 8 + p(0)) * 8)
        # m (SGR), n, h, l, t, q, g, c ... ignored
        if f in "HfABCDEFG`dsuZI":
            self.wrap = False


def _wide(ch: str) -> bool:
    return bool(ch) and unicodedata.east_asian_width(ch) in ("W", "F")


# --------------------------------------------------------------------------- #
# Running a game in a pty
# --------------------------------------------------------------------------- #

class RunResult:
    def __init__(self) -> None:
        self.returncode: int | None = None
        self.raw = b""
        self.text = ""
        self.snaps: dict[str, str] = {}
        self.final_screen = ""
        self.cpu_ratio: float | None = None
        self.cpu_seconds = 0.0
        self.wall = 0.0
        self.timed_out = False
        self.screen: Screen | None = None

    def has_traceback(self) -> bool:
        return "Traceback (most recent call last)" in self.text

    def restored_terminal(self) -> bool:
        return "\x1b[?1049l" in self.text or "\x1b[?25h" in self.text or "\x1b[?47l" in self.text

    def plain_text(self) -> str:
        """Raw output with escape sequences stripped (crude)."""
        return _ANSI_RE.sub("", self.text)


_ANSI_RE = re.compile(r"\x1b\[[0-9;?>=!]*[ -/]*[@-~]|\x1b[()][0-9A-Za-z]|\x1b[=>78DMEHc]|\x1b\][^\x07]*\x07")


def _set_winsize(fd: int, rows: int, cols: int) -> None:
    fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))


def run_game(
    game: str,
    cols: int = 100,
    rows: int = 30,
    steps: list[dict] | None = None,
    extra_args: list[str] | tuple[str, ...] = (),
    exit_timeout: float = 5.0,
    python: str | None = None,
    env_extra: dict[str, str] | None = None,
) -> RunResult:
    """Run `game` in a pty of the given size, drive it with `steps`, return a RunResult."""
    python = python or sys.executable
    master, slave = pty.openpty()
    _set_winsize(slave, rows, cols)
    env = dict(os.environ)
    env.update(TERM="xterm-256color", LANG="en_US.UTF-8", LC_ALL="en_US.UTF-8",
               PYTHONUNBUFFERED="1", PYTHONIOENCODING="utf-8")
    env.pop("COLUMNS", None)
    env.pop("LINES", None)
    # keep test runs from touching the player's real best-score file
    env["FLAPPY_SCORE_FILE"] = os.path.join(tempfile.gettempdir(), "flappy_smoke_score.json")
    if env_extra:
        env.update(env_extra)

    def preexec() -> None:
        os.setsid()
        tiocsctty = getattr(termios, "TIOCSCTTY", None)
        if tiocsctty is not None:
            try:
                fcntl.ioctl(0, tiocsctty, 0)
            except OSError:
                pass

    res = RunResult()
    scr = Screen(rows, cols)
    res.screen = scr
    raw = bytearray()
    dec = codecs.getincrementaldecoder("utf-8")(errors="replace")
    ru0 = resource.getrusage(resource.RUSAGE_CHILDREN)
    t0 = time.monotonic()
    proc = subprocess.Popen(
        [python, game, *extra_args],
        stdin=slave, stdout=slave, stderr=slave, env=env, preexec_fn=preexec, close_fds=True,
    )
    os.close(slave)

    def read_chunk() -> bytes | None:
        try:
            chunk = os.read(master, 65536)
        except OSError:
            return None
        if not chunk:
            return None
        raw.extend(chunk)
        scr.feed(dec.decode(chunk))
        return chunk

    def pump(duration: float) -> bool:
        """Pump output for `duration` seconds. Returns False once the child is gone."""
        end = time.monotonic() + duration
        while True:
            left = end - time.monotonic()
            if left <= 0:
                return proc.poll() is None
            r, _, _ = select.select([master], [], [], min(left, 0.05))
            if r:
                if read_chunk() is None:
                    return False
            elif proc.poll() is not None:
                # drain what's left
                for _ in range(20):
                    r, _, _ = select.select([master], [], [], 0.05)
                    if not r or read_chunk() is None:
                        break
                return False

    alive = True
    for st in steps or []:
        alive = pump(float(st.get("wait", 0.0)))
        if "snap" in st:
            res.snaps[st["snap"]] = scr.text()
        if not alive:
            break
        if "resize" in st:
            r2, c2 = st["resize"]
            _set_winsize(master, r2, c2)
            scr.resize(r2, c2)
            try:
                os.killpg(proc.pid, signal.SIGWINCH)
            except ProcessLookupError:
                pass
        if st.get("keys"):
            try:
                os.write(master, st["keys"])
            except OSError:
                alive = False
                break

    if alive:
        alive = pump(exit_timeout)
    if alive and proc.poll() is None:
        res.timed_out = True
        for sig in (signal.SIGTERM, signal.SIGKILL):
            try:
                os.killpg(proc.pid, sig)
            except ProcessLookupError:
                break
            pump(1.0)
            if proc.poll() is not None:
                break
    # final drain
    for _ in range(20):
        r, _, _ = select.select([master], [], [], 0.05)
        if not r or read_chunk() is None:
            break
    proc.wait()
    os.close(master)
    res.wall = time.monotonic() - t0
    ru1 = resource.getrusage(resource.RUSAGE_CHILDREN)
    res.cpu_seconds = (ru1.ru_utime - ru0.ru_utime) + (ru1.ru_stime - ru0.ru_stime)
    res.cpu_ratio = res.cpu_seconds / res.wall if res.wall > 0 else None
    res.returncode = None if res.timed_out else proc.returncode
    res.raw = bytes(raw)
    res.text = res.raw.decode("utf-8", "replace")
    res.final_screen = scr.text()
    return res


# --------------------------------------------------------------------------- #
# Scenarios
# --------------------------------------------------------------------------- #

UP = b"\x1b[A"
ESC = b"\x1b"
CTRL_C = b"\x03"


def _flaps(n: int, every: float = 0.35) -> list[dict]:
    return [{"wait": every, "keys": b" "} for _ in range(n)]


def play_steps(short: bool = False) -> list[dict]:
    steps = [
        {"wait": 1.2, "snap": "title"},
        {"wait": 0.0, "keys": b" "},
        {"wait": 0.5, "snap": "start"},
        *_flaps(4 if short else 8),
        {"wait": 0.2, "snap": "playing"},
        {"wait": 4.5, "snap": "after_death"},   # no flaps -> the bird must have died by now
        {"wait": 0.0, "keys": b"r"},
        {"wait": 0.6, "snap": "restarted"},
        {"wait": 0.0, "keys": b" "},
        *_flaps(3),
        {"wait": 0.0, "keys": b"p"},
        {"wait": 0.5, "snap": "paused"},
        {"wait": 0.0, "keys": b"p"},
        *_flaps(2),
        {"wait": 0.3, "keys": b"q"},
    ]
    return steps


def _nonblank(s: str) -> int:
    return sum(1 for ch in s if not ch.isspace())


SCORE_RE = re.compile(r"score\D{0,8}(\d+)", re.IGNORECASE)


def _score_in(snap: str) -> int | None:
    m = SCORE_RE.findall(snap)
    return max(int(x) for x in m) if m else None


class Check:
    def __init__(self) -> None:
        self.lines: list[tuple[str, str]] = []  # (level, message)

    def ok(self, cond: bool, msg: str, warn_only: bool = False) -> None:
        self.lines.append(("PASS" if cond else ("WARN" if warn_only else "FAIL"), msg))

    @property
    def failed(self) -> bool:
        return any(level == "FAIL" for level, _ in self.lines)


def common_checks(ck: Check, res: RunResult, expect_exit: int | None = 0, min_cells: int = 40) -> None:
    ck.ok(not res.has_traceback(), "no Python traceback in output")
    ck.ok(not res.timed_out, "process exited on its own (did not hang)")
    if expect_exit is not None:
        ck.ok(res.returncode == expect_exit, f"exit code is {expect_exit} (got {res.returncode})")
    ck.ok(res.restored_terminal(), "terminal restored on exit (alt screen left / cursor shown)")
    for label, snap in res.snaps.items():
        ck.ok(_nonblank(snap) >= min_cells, f"snapshot '{label}' has visible content ({_nonblank(snap)} glyphs)")
    if res.cpu_ratio is not None:
        ck.ok(res.cpu_ratio < 0.6, f"CPU usage sane: {res.cpu_ratio*100:.0f}% of one core")
        ck.ok(res.cpu_ratio < 0.35, f"CPU usage low: {res.cpu_ratio*100:.0f}% of one core (<35%)", warn_only=True)


def scenario_title(game, py):
    res = run_game(game, 100, 30, [{"wait": 1.5, "snap": "title"}, {"wait": 0.0, "keys": b"q"}], python=py)
    ck = Check(); common_checks(ck, res)
    t = res.snaps.get("title", "").lower()
    ck.ok("space" in t or "start" in t or "press" in t, "title screen mentions how to start", warn_only=True)
    return ck, res


def scenario_play(game, py):
    res = run_game(game, 100, 30, play_steps(), python=py)
    ck = Check(); common_checks(ck, res)
    s = res.snaps
    ck.ok(s.get("title") != s.get("playing"), "screen changes between title and play")
    ck.ok("over" in s.get("after_death", "").lower(), "game-over screen shown after the bird falls", warn_only=True)
    paused = s.get("paused", "").lower()
    ck.ok("pause" in paused or "resume" in paused, "pause overlay shows PAUSED or how to resume", warn_only=True)
    return ck, res


def scenario_arrows(game, py):
    steps = [
        {"wait": 1.0, "keys": UP},
        {"wait": 0.3, "keys": UP}, {"wait": 0.3, "keys": b"w"}, {"wait": 0.3, "keys": b"k"},
        {"wait": 0.3, "keys": UP}, {"wait": 0.3, "snap": "playing"},
        {"wait": 0.3, "keys": ESC},
    ]
    res = run_game(game, 100, 30, steps, python=py)
    ck = Check(); common_checks(ck, res)
    return ck, res


def scenario_small(game, py):
    res = run_game(game, 80, 24, play_steps(short=True), python=py)
    ck = Check(); common_checks(ck, res)
    return ck, res


def scenario_tiny(game, py):
    res = run_game(game, 40, 10, [{"wait": 1.2, "snap": "tiny"}, {"wait": 0.0, "keys": b"q"}], python=py)
    ck = Check(); common_checks(ck, res, expect_exit=None, min_cells=10)
    t = res.snaps.get("tiny", "").lower()
    ck.ok("small" in t or "resize" in t or "enlarge" in t, "too-small terminal shows a hint instead of crashing", warn_only=True)
    ck.ok(res.returncode is not None, "process exited after q on a too-small terminal")
    return ck, res


def scenario_resize(game, py):
    steps = [
        {"wait": 1.0, "keys": b" "},
        {"wait": 0.8, "resize": (40, 120)}, {"wait": 0.3, "keys": b" "},
        {"wait": 0.6, "snap": "bigger"}, {"wait": 0.0, "keys": b" "},
        {"wait": 0.4, "resize": (18, 60)}, {"wait": 0.3, "keys": b" "},
        {"wait": 0.6, "snap": "smaller"}, {"wait": 0.0, "keys": b" "},
        {"wait": 0.4, "resize": (10, 40)},
        {"wait": 0.8, "snap": "too_small"},
        {"wait": 0.0, "resize": (30, 100)},
        {"wait": 0.8, "snap": "restored"}, {"wait": 0.0, "keys": b" "},
        {"wait": 0.4, "keys": b" "},
        {"wait": 0.4, "keys": b"q"},
    ]
    res = run_game(game, 100, 30, steps, python=py)
    ck = Check(); common_checks(ck, res, min_cells=10)
    return ck, res


def scenario_ctrlc(game, py):
    steps = [{"wait": 1.0, "keys": b" "}, {"wait": 0.5, "keys": b" "}, {"wait": 0.5, "keys": CTRL_C}]
    res = run_game(game, 100, 30, steps, python=py)
    ck = Check(); common_checks(ck, res, expect_exit=None)
    ck.ok(res.returncode is not None, "process exits on Ctrl-C")
    return ck, res


def scenario_ascii(game, py):
    res = run_game(game, 100, 30, play_steps(short=True), extra_args=["--ascii", "--no-color", "--seed", "3"], python=py)
    ck = Check(); common_checks(ck, res)
    for label in ("title", "playing", "after_death"):
        snap = res.snaps.get(label, "")
        bad = sorted({ch for ch in snap if ord(ch) > 127})
        ck.ok(not bad, f"--ascii: snapshot '{label}' is pure ASCII (non-ascii: {''.join(bad)[:20]!r})")
    return ck, res


def scenario_demo(game, py):
    steps = [{"wait": 3.0, "snap": "d1"}, {"wait": 5.0, "snap": "d2"}, {"wait": 5.0, "snap": "d3"}, {"wait": 0.0, "keys": b"q"}]
    res = run_game(game, 100, 30, steps, extra_args=["--demo", "--seed", "7"], python=py)
    ck = Check(); common_checks(ck, res)
    s1, s3 = _score_in(res.snaps.get("d1", "")), _score_in(res.snaps.get("d3", ""))
    ck.ok(s3 is not None, f"demo shows a plain-text 'score N' (found: {s3})")
    ck.ok(s3 is not None and s3 >= 2, f"demo autopilot scores >= 2 within 13 s (got {s3})")
    ck.ok(s1 is not None and s3 is not None and s3 > s1, f"demo score increases over time ({s1} -> {s3})", warn_only=True)
    ck.ok("demo" in res.snaps.get("d2", "").lower(), "demo mode is labelled on screen", warn_only=True)
    return ck, res


def scenario_help(game, py):
    res = run_game(game, 100, 30, [{"wait": 1.0}], extra_args=["--help"], python=py, exit_timeout=3.0)
    ck = Check()
    ck.ok(not res.has_traceback(), "no traceback on --help")
    ck.ok(res.returncode == 0, f"--help exits 0 (got {res.returncode})")
    ck.ok("usage" in res.plain_text().lower(), "--help prints usage")
    return ck, res


def scenario_cpu(game, py):
    steps = [{"wait": 1.0, "keys": b" "}, *_flaps(14, 0.4), {"wait": 0.2, "keys": b"q"}]
    res = run_game(game, 100, 30, steps, python=py)
    ck = Check(); common_checks(ck, res)
    return ck, res


def scenario_seed(game, py):
    """Same seed -> identical screens at the same moment (determinism for --seed)."""
    steps = [{"wait": 2.5, "snap": "s"}, {"wait": 0.0, "keys": b"q"}]
    a = run_game(game, 100, 30, steps, extra_args=["--demo", "--seed", "11"], python=py)
    b = run_game(game, 100, 30, steps, extra_args=["--demo", "--seed", "11"], python=py)
    ck = Check(); common_checks(ck, a); common_checks(ck, b)
    pa, pb = a.snaps.get("s", ""), b.snaps.get("s", "")
    # compare only the pipe layout roughly: same set of non-empty lines modulo timing jitter is hard;
    # we just require both to be non-empty and warn if they differ a lot
    same = sum(1 for x, y in zip(pa.splitlines(), pb.splitlines()) if x == y)
    total = max(1, len(pa.splitlines()))
    ck.ok(same / total > 0.5, f"--seed gives a reproducible world ({same}/{total} identical lines)", warn_only=True)
    return ck, a


def scenario_py39(game, py):
    alt = "/usr/bin/python3"
    ck = Check()
    if py == alt or not os.path.exists(alt):
        ck.ok(True, "no alternate /usr/bin/python3 to test; skipped")
        return ck, None
    try:
        v = subprocess.run([alt, "-c", "import sys;print(sys.version_info[:2])"], capture_output=True, text=True, timeout=10).stdout.strip()
    except Exception as e:  # noqa: BLE001
        ck.ok(True, f"alternate python not runnable ({e}); skipped")
        return ck, None
    res = run_game(game, 100, 30, play_steps(short=True), python=alt)
    common_checks(ck, res)
    ck.lines.insert(0, ("PASS", f"also ran under {alt} {v}"))
    return ck, res


SCENARIOS = {
    "help": scenario_help,
    "title": scenario_title,
    "play": scenario_play,
    "arrows": scenario_arrows,
    "small": scenario_small,
    "tiny": scenario_tiny,
    "resize": scenario_resize,
    "ctrlc": scenario_ctrlc,
    "ascii": scenario_ascii,
    "demo": scenario_demo,
    "seed": scenario_seed,
    "cpu": scenario_cpu,
    "py39": scenario_py39,
}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("game")
    ap.add_argument("--show", action="store_true", help="print every screen snapshot")
    ap.add_argument("--only", action="append", help="run only this scenario (repeatable)")
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--python", default=sys.executable, help="interpreter to run the game with")
    a = ap.parse_args()
    if a.list:
        print("\n".join(SCENARIOS))
        return 0
    if not os.path.exists(a.game):
        print(f"no such file: {a.game}")
        return 2
    names = a.only or list(SCENARIOS)
    any_fail = False
    for name in names:
        fn = SCENARIOS.get(name)
        if fn is None:
            print(f"unknown scenario {name!r}; choices: {', '.join(SCENARIOS)}")
            return 2
        t0 = time.monotonic()
        try:
            ck, res = fn(a.game, a.python)
        except Exception as e:  # noqa: BLE001
            print(f"== {name}: HARNESS ERROR {e!r}")
            any_fail = True
            continue
        status = "FAIL" if ck.failed else "ok"
        print(f"== {name}: {status}  ({time.monotonic()-t0:.1f}s)")
        for level, msg in ck.lines:
            if level != "PASS" or a.show:
                print(f"   [{level}] {msg}")
        if ck.failed:
            any_fail = True
        if res is not None and (a.show or ck.failed):
            if res.has_traceback():
                tb = res.plain_text()
                i = tb.find("Traceback")
                print("   --- traceback ---")
                print("   " + tb[i:i + 2500].replace("\n", "\n   "))
            if a.show:
                for label, snap in res.snaps.items():
                    print(f"   --- {name}/{label} ---")
                    print("\n".join("   " + line for line in snap.splitlines()))
                print(f"   --- {name}/final ---")
                print("\n".join("   " + line for line in res.final_screen.splitlines()))
    print("RESULT:", "FAIL" if any_fail else "ALL PASS")
    return 1 if any_fail else 0


if __name__ == "__main__":
    sys.exit(main())
