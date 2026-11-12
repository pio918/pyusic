"""Audio visualizer: capture pyusic's own audio from PipeWire, analyse it, draw it as text.

How it works
------------
1. mpv is started with the client name ``pyusic-mpv``. We open a capture stream
   (``pyusic-viz``, via pw-cat) and link mpv's output ports to it with pw-link.
   Audio still reaches the speakers; the capture only sees pyusic, never other apps.
2. Every frame we take the last 2048 samples, apply a Hann window and an FFT. That
   splits the sound by frequency; each bar is the energy of one band (log-spaced:
   bass on the left, treble on the right).
3. An automatic gain follows the loudness (like cava's autosens), so the bars are
   useful at any volume. Bars rise fast and fall slowly, with falling peak caps.

Modes: bars, thin (braille, 1 dot per bar), mirror, wave (oscilloscope), vu.
"""
from __future__ import annotations

import asyncio
import collections
import colorsys
import ctypes
import functools
import os
import shutil
import signal
import time

import numpy as np
from rich.text import Text

RATE = 44100
FFT = 2048
MODES = ["bars", "thin", "mirror", "wave", "vu", "off"]
COLORS = ["theme", "rainbow", "fire", "ice", "mono"]
# Names are per process, so two running pyusic instances never link to each other's streams.
MPV_CLIENT = f"pyusic-mpv-{os.getpid()}"
VIZ_NODE = f"pyusic-viz-{os.getpid()}"

_HANN = np.hanning(FFT).astype(np.float32)
_BLOCKS = " ▁▂▃▄▅▆▇█"
_LEFT_DOTS = (0x01, 0x02, 0x04, 0x40)   # braille left column, top to bottom
_RIGHT_DOTS = (0x08, 0x10, 0x20, 0x80)


def die_with_parent() -> None:
    """preexec_fn: if pyusic is killed hard, take the child (pw-cat / mpv) down with it."""
    try:
        ctypes.CDLL("libc.so.6", use_errno=True).prctl(1, signal.SIGTERM)  # PR_SET_PDEATHSIG
    except Exception:
        pass


# --------------------------------------------------------------------------- colours


def _rgb(h: str) -> tuple[int, int, int]:
    h = h.lstrip("#")
    return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)


def _lerp(a: str, b: str, t: float) -> str:
    t = max(0.0, min(1.0, t))
    (r1, g1, b1), (r2, g2, b2) = _rgb(a), _rgb(b)
    return f"#{round(r1 + (r2 - r1) * t):02x}{round(g1 + (g2 - g1) * t):02x}{round(b1 + (b2 - b1) * t):02x}"


_flash = 0.0


def set_flash(amount: float) -> None:
    """Beat flash for the frame being drawn: 0 = normal colours, 1 = pushed towards white."""
    global _flash
    _flash = max(0.0, min(1.0, amount))


_PAL_KEYS = ("accent", "magenta", "yellow", "red", "cyan", "foreground", "muted", "dark_background")
_B = 24   # colour buckets across the width: gradients are smooth enough, and a row needs <= 24 text spans


def color_at(scheme: str, pal: dict, x: float, y: float) -> str:
    """Colour for a cell: x = 0..1 across the width, y = 0..1 up the bar (then the beat flash)."""
    c = _scheme_color(scheme, pal, x, y)
    return _lerp(c, pal["foreground"], _flash * 0.6) if _flash > 0.02 else c


@functools.lru_cache(maxsize=256)
def _grid(scheme: str, ys: tuple, flashq: int, pal_key: tuple) -> list:
    pal = dict(zip(_PAL_KEYS, pal_key))
    f = flashq / 8
    rows = []
    for y in ys:
        row = []
        for b in range(_B):
            c = _scheme_color(scheme, pal, (b + 0.5) / _B, y)
            row.append(_lerp(c, pal["foreground"], f * 0.6) if f > 0.02 else c)
        rows.append(row)
    return rows


def color_grid(scheme: str, pal: dict, ys) -> list:
    """colours[row][bucket] for one layout. Cached: it only changes with the size, scheme, beat flash or theme."""
    return _grid(scheme, tuple(round(y, 3) for y in ys), int(round(_flash * 8)), tuple(pal[k] for k in _PAL_KEYS))


def _scheme_color(scheme: str, pal: dict, x: float, y: float) -> str:
    if scheme == "rainbow":
        r, g, b = colorsys.hsv_to_rgb((x * 0.85) % 1.0, 0.65, 1.0)
        return f"#{int(r * 255):02x}{int(g * 255):02x}{int(b * 255):02x}"
    if scheme == "fire":
        return _lerp(pal["yellow"], pal["red"], y)
    if scheme == "ice":
        return _lerp(pal["cyan"], pal["magenta"], y * 0.9 + x * 0.1)
    if scheme == "mono":
        return _lerp(pal["muted"], pal["foreground"], y)
    return _lerp(pal["accent"], pal["magenta"], y)  # "theme"


# --------------------------------------------------------------------------- analysis


class Analyzer:
    def __init__(self) -> None:
        self.mono = np.zeros(FFT * 2, np.float32)
        self.level_l = self.level_r = 0.0
        self.proc: asyncio.subprocess.Process | None = None
        self.tasks: list[asyncio.Task] = []
        self.available = False
        self.want_link = False
        self.error = ""
        self.bytes_read = 0
        self._wanted = False
        self._linked = False
        self._reader: asyncio.Task | None = None
        self._lock = asyncio.Lock()
        self._apply_task: asyncio.Future | None = None
        self._ref = 0.01          # automatic gain reference (spectrum)
        self._wave_ref = 0.02
        self._vu_ref = 0.02
        self._bars = np.zeros(0)
        self._peaks = np.zeros(0)
        self._edges: dict[int, tuple] = {}
        self.vu_l = self.vu_r = self.vu_pl = self.vu_pr = 0.0
        self._bass_hist: collections.deque = collections.deque(maxlen=30)   # ~1.2 s at 25 fps
        self._beat_ref = 0.0
        self._beat = 0.0
        self._last_beat = 0.0

    # ---- capture ----
    #
    # The PipeWire stream is opened on demand: only while the visualizer is on and music is playing.
    # Left running it would wake up ~50 times a second to process silence.

    async def start(self) -> bool:
        """Check that capturing is possible. The stream itself is opened by set_wanted()."""
        if not shutil.which("pw-cat") or not shutil.which("pw-link"):
            self.error = "PipeWire tools (pw-cat, pw-link) not found"
            return False
        self.available = True
        self.tasks = [asyncio.create_task(self._linker())]
        return True

    def set_wanted(self, flag: bool) -> None:
        if flag != self._wanted:
            self._wanted = flag
            self._apply_task = asyncio.ensure_future(self._apply())

    def kick(self) -> None:
        """A new stream is starting (next track): look for its ports again right away."""
        self._linked = False

    async def _apply(self) -> None:
        async with self._lock:
            if self._wanted and (self.proc is None or self.proc.returncode is not None):
                await self._spawn()
            elif not self._wanted:
                await self._despawn()

    async def _spawn(self) -> None:
        try:
            self.proc = await asyncio.create_subprocess_exec(
                "pw-cat", "--record", "--raw", "--format", "f32", "--rate", str(RATE), "--channels", "2",
                "--latency", "50ms",
                "-P", f"{{ node.name={VIZ_NODE} node.autoconnect=false node.passive=true "
                      f"node.description=pyusic-visualizer }}", "-",
                stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL, preexec_fn=die_with_parent,
            )
        except OSError as e:
            self.error, self.proc = str(e), None
            return
        self._linked = False
        self._reader = asyncio.create_task(self._read(self.proc))

    async def _despawn(self) -> None:
        if self._reader:
            self._reader.cancel()
            self._reader = None
        proc, self.proc = self.proc, None
        if proc and proc.returncode is None:
            proc.terminate()
            try:
                await asyncio.wait_for(proc.wait(), 2)
            except asyncio.TimeoutError:
                proc.kill()
        self.mono[:] = 0
        self.level_l = self.level_r = 0.0

    async def close(self) -> None:
        self._wanted = False
        for t in self.tasks:
            t.cancel()
        async with self._lock:
            await self._despawn()

    async def _read(self, proc: asyncio.subprocess.Process) -> None:
        assert proc.stdout
        rest = b""
        size = len(self.mono)
        while chunk := await proc.stdout.read(32768):
            self.bytes_read += len(chunk)
            data = rest + chunk
            n = len(data) // 8 * 8
            rest = data[n:]
            if not n:
                continue
            a = np.frombuffer(data[:n], dtype=np.float32).reshape(-1, 2)
            m = a.mean(axis=1)
            self.mono = m[-size:].copy() if len(m) >= size else np.concatenate((self.mono[len(m):], m))
            self.level_l, self.level_r = np.sqrt((a * a).mean(axis=0)).tolist()
        if self._wanted:
            self.error = "capture stopped"

    async def _linker(self) -> None:
        """Keep mpv's output ports linked to our capture node (mpv recreates its ports per stream)."""
        want = [(f"{MPV_CLIENT}:output_FL", f"{VIZ_NODE}:input_FL"), (f"{MPV_CLIENT}:output_FR", f"{VIZ_NODE}:input_FR")]
        while True:
            await asyncio.sleep(1.5 if self._linked else 0.25)
            if not (self.want_link and self.proc is not None):
                continue
            try:
                p = await asyncio.create_subprocess_exec(
                    "pw-link", "-l", stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
                out = (await p.communicate())[0].decode(errors="replace")
                links: set[tuple[str, str]] = set()
                cur = ""
                for line in out.splitlines():
                    if not line.startswith(" "):
                        cur = line.strip()
                    elif line.strip().startswith("|->"):
                        links.add((cur, line.strip()[3:].strip()))
                ok = True
                for src, dst in want:
                    if (src, dst) in links:
                        continue
                    ok = False
                    if src in out:        # mpv's port is up (-l lists only linked ports, so ours can't be checked): link it
                        q = await asyncio.create_subprocess_exec(
                            "pw-link", src, dst, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
                        await q.wait()
                self._linked = ok
            except OSError:
                pass

    # ---- spectrum ----

    def _band_edges(self, n: int) -> tuple:
        if n not in self._edges:
            edges = np.geomspace(40, 16000, n + 1)
            bin_hz = RATE / FFT
            lo = np.floor(edges[:-1] / bin_hz).astype(int)
            hi = np.maximum(lo + 1, np.ceil(edges[1:] / bin_hz).astype(int))
            centers = np.sqrt(edges[:-1] * edges[1:])
            tilt = np.clip((centers / 400.0) ** 0.5, 0.3, 6.0)   # ~ +3 dB/octave: music falls off with pitch
            idx = np.empty(n * 2, dtype=np.intp)       # interleaved [lo0, hi0, lo1, hi1, ...] for reduceat
            idx[0::2], idx[1::2] = lo, np.minimum(hi, FFT // 2)
            self._edges[n] = (idx, tilt)
        return self._edges[n]

    def update_spectrum(self, n: int, gain: float) -> tuple[np.ndarray, np.ndarray]:
        """Advance one frame: returns (bars, peaks), both 0..1, length n."""
        if len(self._bars) != n:
            self._bars, self._peaks = np.zeros(n), np.zeros(n)
        x = self.mono[-FFT:] * _HANN
        mag = np.abs(np.fft.rfft(x)) / (FFT / 4)
        idx, tilt = self._band_edges(n)
        raw = np.maximum.reduceat(mag, idx)[0::2] * tilt     # max of each band's bins, no Python loop
        peak = float(raw.max()) if n else 0.0
        self._ref = max(self._ref * 0.9985, peak, 0.004)       # slow automatic gain
        v = np.clip((raw / self._ref) ** 0.8 * gain, 0.0, 1.0)
        rising = v > self._bars
        self._bars = np.where(rising, self._bars + (v - self._bars) * 0.75, np.maximum(v, self._bars * 0.88))
        self._peaks = np.maximum(self._bars, self._peaks - 0.015)
        return self._bars, self._peaks

    def update_beat(self) -> float:
        """Kick detector: 1.0 on a beat, decaying towards 0 between beats.

        Looks at the energy in the bass (40-170 Hz). A beat is a frame where that energy jumps well above
        its average (and its normal wobble) over the last ~second, at least 0.2 s after the previous one."""
        x = self.mono[-FFT:] * _HANN
        mag = np.abs(np.fft.rfft(x)) / (FFT / 4)
        e = float(np.sqrt((mag[2:9] ** 2).mean()))
        avg = sum(self._bass_hist) / len(self._bass_hist) if self._bass_hist else 0.0
        self._beat_ref = max(self._beat_ref * 0.998, e, 0.003)
        now = time.monotonic()
        spread = float(np.std(self._bass_hist)) if len(self._bass_hist) > 5 else 0.0
        # tuned on synthetic kicks (60-140 bpm, with and without hiss): finds ~all kicks, ~no false beats
        if (self._bass_hist and e > avg * 1.6 and e > avg + 2.0 * spread
                and e > 0.3 * self._beat_ref and now - self._last_beat > 0.2):
            self._beat, self._last_beat = 1.0, now
        else:
            self._beat *= 0.78
        self._bass_hist.append(e)
        return self._beat if self._beat > 0.03 else 0.0

    def wave(self, n: int, gain: float) -> np.ndarray:
        """n samples of the waveform in -1..1 (automatic gain)."""
        seg = self.mono[-FFT:]
        idx = np.linspace(0, len(seg) - 1, n).astype(int)
        s = seg[idx].astype(np.float64)
        self._wave_ref = max(self._wave_ref * 0.998, float(np.abs(s).max()), 0.01)
        return np.clip(s / self._wave_ref * gain * 0.9, -1.0, 1.0)

    def update_vu(self, gain: float) -> tuple[float, float, float, float]:
        top = max(self.level_l, self.level_r)
        self._vu_ref = max(self._vu_ref * 0.999, top, 0.01)
        l = min(1.0, (self.level_l / self._vu_ref) ** 0.8 * gain)
        r = min(1.0, (self.level_r / self._vu_ref) ** 0.8 * gain)
        self.vu_l = l if l > self.vu_l else self.vu_l * 0.85
        self.vu_r = r if r > self.vu_r else self.vu_r * 0.85
        self.vu_pl = max(self.vu_l, self.vu_pl - 0.012)
        self.vu_pr = max(self.vu_r, self.vu_pr - 0.012)
        return self.vu_l, self.vu_r, self.vu_pl, self.vu_pr


# --------------------------------------------------------------------------- drawing
#
# Every frame is built from whole strings, not cell by cell: per row the glyphs are produced in bulk
# (numpy / list comprehensions) and colours come from a cached per-row gradient, so a row costs a handful
# of text spans instead of one per cell. That is what keeps a full-width visualizer cheap.


def _blank(w: int) -> Text:
    return Text(" " * w)


def _runs(t: Text, items) -> None:
    """Append (text, style) pieces, merging neighbours that share a style."""
    cur, buf = None, []
    for piece, style in items:
        if style != cur and buf:
            t.append("".join(buf), style=cur)
            buf = []
        cur = style
        buf.append(piece)
    if buf:
        t.append("".join(buf), style=cur)


@functools.lru_cache(maxsize=32)
def _bucket_edges(w: int) -> tuple:
    edges = []
    for b in range(_B):
        a, z = b * w // _B, (b + 1) * w // _B
        if z > a:
            edges.append((a, z, b))
    return tuple(edges)


def _cells_to_text(row: str, row_colors: list[str], w: int) -> Text:
    """One text row from a string of w glyphs, coloured by bucket across the width (at most _B spans)."""
    t = Text()
    _runs(t, [(row[a:z], row_colors[b]) for a, z, b in _bucket_edges(w)])
    return t


def draw_bars(bars, peaks, w: int, h: int, scheme: str, pal: dict) -> list[Text]:
    """Classic spectrum: bars two cells wide, one gap, with falling peak caps."""
    n = len(bars)
    span = n * 3 - 1
    pad = max(0, (w - span) // 2)
    grid = color_grid(scheme, pal, [(h - 1 - r + 0.5) / h for r in range(h)])
    eighths = (bars * h * 8).astype(int)
    caps = np.minimum((peaks * h).astype(int), h - 1)
    capok = peaks > 0.04
    lines: list[Text] = []
    for r in range(h):
        k = h - 1 - r
        fill = np.clip(eighths - k * 8, 0, 8).tolist()
        cap = ((caps == k) & capok).tolist()
        row = grid[r]
        items = []
        for i in range(n):
            f = fill[i]
            g = _BLOCKS[f] * 2 if f else ("▂▂" if cap[i] else "  ")
            items.append((g + (" " if i < n - 1 else ""), row[i * _B // n]))
        t = Text(" " * pad)
        _runs(t, items)
        t.append(" " * max(0, w - pad - span))
        lines.append(t)
    return lines


def _braille_rows(bits: np.ndarray) -> list[str]:
    """bits: (rows, w) braille dot masks -> one string per row (empty cells become spaces)."""
    codes = np.where(bits == 0, 0x20, bits + 0x2800).astype("<u4")
    return [codes[r].tobytes().decode("utf-32-le") for r in range(codes.shape[0])]


_LW = np.array(_LEFT_DOTS)
_RW = np.array(_RIGHT_DOTS)


def draw_thin(bars, peaks, w: int, h: int, scheme: str, pal: dict) -> list[Text]:
    """Braille spectrum: one dot-column per bar, four dot-rows per text row."""
    n = w * 2
    dots = np.zeros(n, int)
    dots[: len(bars)] = (bars * h * 4).astype(int)[:n]
    caps = np.zeros(n, int)
    caps[: len(peaks)] = np.minimum((peaks * h * 4).astype(int), h * 4 - 1)[:n]
    capok = np.zeros(n, bool)
    capok[: len(peaks)] = peaks[:n] > 0.05
    dl, dr, cl, cr, ol, orr = dots[0::2], dots[1::2], caps[0::2], caps[1::2], capok[0::2], capok[1::2]
    grid = color_grid(scheme, pal, [((h - 1 - r) * 4 + 2) / (h * 4) for r in range(h)])
    lvl = (((h - 1 - np.arange(h)) * 4)[:, None] + (3 - np.arange(4))[None, :])[:, :, None]   # (h, 4, 1) dot heights
    lit_l = (lvl < dl) | ((lvl == cl) & ol)                                                  # (h, 4, w)
    lit_r = (lvl < dr) | ((lvl == cr) & orr)
    bits = (lit_l * _LW[None, :, None] + lit_r * _RW[None, :, None]).sum(axis=1)             # (h, w)
    rows = _braille_rows(bits)
    return [_cells_to_text(rows[r], grid[r], w) for r in range(h)]


def draw_mirror(bars, peaks, w: int, h: int, scheme: str, pal: dict) -> list[Text]:
    """Bars growing both ways from a centre line."""
    n = len(bars)
    half = max(1, h // 2)
    span = n * 3 - 1
    pad = max(0, (w - span) // 2)
    eighths = (bars * half * 8).astype(int)
    bg = pal["dark_background"]
    grid = color_grid(scheme, pal, [(k + 0.5) / half for k in range(half)])
    lines: list[Text] = []
    for r in range(h):
        up = r < half
        k = (half - 1 - r) if up else (r - (h - half))   # distance from the centre line, in rows
        if k < 0:
            lines.append(_blank(w))
            continue
        fill = np.clip(eighths - k * 8, 0, 8).tolist()
        row = grid[min(k, half - 1)]
        items = []
        for i in range(n):
            f, col = fill[i], row[i * _B // n]
            if not f:
                items.append(("   " if i < n - 1 else "  ", col))
            elif up or f == 8:
                items.append((_BLOCKS[f] * 2 + (" " if i < n - 1 else ""), col))
            else:   # lower half: the bar hangs from the top of the cell, so invert a bottom-filled block
                items.append((_BLOCKS[8 - f] * 2, f"{bg} on {col}"))
                if i < n - 1:
                    items.append((" ", col))
        t = Text(" " * pad)
        _runs(t, items)
        t.append(" " * max(0, w - pad - span))
        lines.append(t)
    return lines


_DOT_WEIGHTS = np.array([[0x01, 0x08], [0x02, 0x10], [0x04, 0x20], [0x40, 0x80]])


def draw_wave(samples, w: int, h: int, scheme: str, pal: dict) -> list[Text]:
    """Oscilloscope in braille (2x4 dots per cell)."""
    W, H = w * 2, h * 4
    ys = ((1 - np.resize(samples, W)) / 2 * (H - 1)).round().astype(int)
    prev = np.concatenate(([ys[0]], ys[:-1]))
    lo, hi = np.minimum(prev, ys)[None, :], np.maximum(prev, ys)[None, :]
    rows = np.arange(H)[:, None]
    grid = (rows >= lo) & (rows <= hi)                                     # (H, W): vertical strokes joining the samples
    bits = (grid.reshape(h, 4, w, 2) * _DOT_WEIGHTS[None, :, None, :]).sum(axis=(1, 3))
    mid = H // 2
    colors = color_grid(scheme, pal, [abs(((r * 4 + 2) - mid) / max(1, mid)) for r in range(h)])
    strs = _braille_rows(bits)
    return [_cells_to_text(strs[r], colors[r], w) for r in range(h)]


def draw_vu(levels, w: int, h: int, scheme: str, pal: dict) -> list[Text]:
    """Two stereo level meters with peak hold."""
    l, r, pl, pr = levels
    width = max(4, w - 3)
    lines = [_blank(w) for _ in range(h)]
    rows = [(max(0, h // 2 - 1), "L", l, pl), (min(h - 1, h // 2 + 1), "R", r, pr)] if h >= 3 else [(0, "L", l, pl)]
    grid = color_grid(scheme, pal, [0.5])[0]
    for row, label, v, pk in rows:
        cells = v * width
        full = int(cells)
        peak_at = min(width - 1, int(pk * width))
        items = [(label + " ", pal["muted"])]
        for i in range(width):
            col = grid[i * _B // width]
            if i < full:
                items.append(("█", col))
            elif i == full and cells - full > 0.5:
                items.append(("▌", col))
            elif i == peak_at and pk > 0.05:
                items.append(("▎", col))
            else:
                items.append(("·", pal["muted"]))
        t = Text()
        _runs(t, items)
        t.append(" " * max(0, w - 2 - width))
        lines[row] = t
    return lines
