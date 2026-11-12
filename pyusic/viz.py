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
import colorsys
import ctypes
import os
import shutil
import signal

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


def color_at(scheme: str, pal: dict, x: float, y: float) -> str:
    """Colour for a cell: x = 0..1 across the width, y = 0..1 up the bar."""
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
        self._ref = 0.01          # automatic gain reference (spectrum)
        self._wave_ref = 0.02
        self._vu_ref = 0.02
        self._bars = np.zeros(0)
        self._peaks = np.zeros(0)
        self._edges: dict[int, tuple] = {}
        self.vu_l = self.vu_r = self.vu_pl = self.vu_pr = 0.0

    # ---- capture ----

    async def start(self) -> bool:
        if not shutil.which("pw-cat") or not shutil.which("pw-link"):
            self.error = "PipeWire tools (pw-cat, pw-link) not found"
            return False
        try:
            self.proc = await asyncio.create_subprocess_exec(
                "pw-cat", "--record", "--raw", "--format", "f32", "--rate", str(RATE), "--channels", "2",
                "--latency", "30ms",
                "-P", f"{{ node.name={VIZ_NODE} node.autoconnect=false node.passive=true "
                      f"node.description=pyusic-visualizer }}", "-",
                stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL, preexec_fn=die_with_parent,
            )
        except OSError as e:
            self.error = str(e)
            return False
        self.available = True
        self.tasks = [asyncio.create_task(self._read()), asyncio.create_task(self._linker())]
        return True

    async def close(self) -> None:
        for t in self.tasks:
            t.cancel()
        if self.proc and self.proc.returncode is None:
            self.proc.terminate()
            try:
                await asyncio.wait_for(self.proc.wait(), 2)
            except asyncio.TimeoutError:
                self.proc.kill()

    async def _read(self) -> None:
        assert self.proc and self.proc.stdout
        rest = b""
        size = len(self.mono)
        while chunk := await self.proc.stdout.read(16384):
            self.bytes_read += len(chunk)
            data = rest + chunk
            n = len(data) // 8 * 8
            rest = data[n:]
            if not n:
                continue
            a = np.frombuffer(data[:n], dtype=np.float32).reshape(-1, 2)
            m = a.mean(axis=1)
            self.mono = m[-size:].copy() if len(m) >= size else np.concatenate((self.mono[len(m):], m))
            rl, rr = float(np.sqrt((a[:, 0] ** 2).mean())), float(np.sqrt((a[:, 1] ** 2).mean()))
            self.level_l, self.level_r = rl, rr
        self.available = False
        self.error = "capture stopped"

    async def _linker(self) -> None:
        """Keep mpv's output ports linked to our capture node (mpv recreates them per stream)."""
        want = [(f"{MPV_CLIENT}:output_FL", f"{VIZ_NODE}:input_FL"), (f"{MPV_CLIENT}:output_FR", f"{VIZ_NODE}:input_FR")]
        while True:
            await asyncio.sleep(0.8)
            if not self.want_link:
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
                for src, dst in want:
                    if (src, dst) not in links and any(c == src for c, _ in links) | (src in out):
                        q = await asyncio.create_subprocess_exec(
                            "pw-link", src, dst, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
                        await q.wait()
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
            self._edges[n] = (lo, hi, tilt)
        return self._edges[n]

    def update_spectrum(self, n: int, gain: float) -> tuple[np.ndarray, np.ndarray]:
        """Advance one frame: returns (bars, peaks), both 0..1, length n."""
        if len(self._bars) != n:
            self._bars, self._peaks = np.zeros(n), np.zeros(n)
        x = self.mono[-FFT:] * _HANN
        mag = np.abs(np.fft.rfft(x)) / (FFT / 4)
        lo, hi, tilt = self._band_edges(n)
        raw = np.fromiter((mag[a:b].max() for a, b in zip(lo, hi)), dtype=np.float64, count=n) * tilt
        peak = float(raw.max()) if n else 0.0
        self._ref = max(self._ref * 0.9985, peak, 0.004)       # slow automatic gain
        v = np.clip((raw / self._ref) ** 0.8 * gain, 0.0, 1.0)
        rising = v > self._bars
        self._bars = np.where(rising, self._bars + (v - self._bars) * 0.75, np.maximum(v, self._bars * 0.88))
        self._peaks = np.maximum(self._bars, self._peaks - 0.015)
        return self._bars, self._peaks

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


def _blank(w: int) -> Text:
    return Text(" " * w)


def draw_bars(bars, peaks, w: int, h: int, scheme: str, pal: dict) -> list[Text]:
    """Classic spectrum: bars two cells wide, one gap, with falling peak caps."""
    n = len(bars)
    pad = max(0, (w - (n * 3 - 1)) // 2)
    lines: list[Text] = []
    eighths = (bars * h * 8).astype(int)
    caps = np.minimum((peaks * h).astype(int), h - 1)
    for r in range(h):
        k = h - 1 - r  # rows above the floor
        t = Text(" " * pad)
        for i in range(n):
            fill = int(min(8, max(0, eighths[i] - k * 8)))
            x = i / max(1, n - 1)
            if fill:
                t.append(_BLOCKS[fill] * 2, style=color_at(scheme, pal, x, (k + 0.5) / h))
            elif peaks[i] > 0.04 and caps[i] == k:
                t.append("▂▂", style=color_at(scheme, pal, x, min(1.0, peaks[i])))
            else:
                t.append("  ")
            if i < n - 1:
                t.append(" ")
        t.append(" " * max(0, w - pad - (n * 3 - 1)))
        lines.append(t)
    return lines


def draw_thin(bars, peaks, w: int, h: int, scheme: str, pal: dict) -> list[Text]:
    """Braille spectrum: one dot-column per bar, four dot-rows per text row."""
    dots = (bars * h * 4).astype(int)
    caps = np.minimum((peaks * h * 4).astype(int), h * 4 - 1)
    lines: list[Text] = []
    for r in range(h):
        t = Text()
        base = (h - 1 - r) * 4
        for c in range(w):
            bits = 0
            for side, table in ((0, _LEFT_DOTS), (1, _RIGHT_DOTS)):
                i = c * 2 + side
                if i >= len(dots):
                    continue
                for j in range(4):
                    level = base + (3 - j)
                    if level < dots[i] or (level == caps[i] and peaks[i] > 0.05):
                        bits |= table[j]
            if bits:
                x = c / max(1, w - 1)
                t.append(chr(0x2800 + bits), style=color_at(scheme, pal, x, (base + 2) / (h * 4)))
            else:
                t.append(" ")
        lines.append(t)
    return lines


def draw_mirror(bars, peaks, w: int, h: int, scheme: str, pal: dict) -> list[Text]:
    """Bars growing both ways from a centre line."""
    n = len(bars)
    half = max(1, h // 2)
    pad = max(0, (w - (n * 3 - 1)) // 2)
    eighths = (bars * half * 8).astype(int)
    bg = pal["dark_background"]
    lines: list[Text] = []
    for r in range(h):
        up = r < half
        k = (half - 1 - r) if up else (r - (h - half))   # distance from the centre line, in rows
        if not up and r - (h - half) < 0:
            lines.append(_blank(w))
            continue
        t = Text(" " * pad)
        for i in range(n):
            fill = int(min(8, max(0, eighths[i] - k * 8)))
            x = i / max(1, n - 1)
            col = color_at(scheme, pal, x, (k + 0.5) / half)
            if not fill:
                t.append("  ")
            elif up or fill == 8:
                t.append(_BLOCKS[fill] * 2, style=col)
            else:  # lower half: the bar hangs from the top of the cell, so invert a bottom-filled block
                t.append(_BLOCKS[8 - fill] * 2, style=f"{bg} on {col}")
            if i < n - 1:
                t.append(" ")
        t.append(" " * max(0, w - pad - (n * 3 - 1)))
        lines.append(t)
    return lines


def draw_wave(samples, w: int, h: int, scheme: str, pal: dict) -> list[Text]:
    """Oscilloscope in braille (2x4 dots per cell)."""
    W, H = w * 2, h * 4
    grid = np.zeros((H, W), dtype=bool)
    ys = ((1 - samples) / 2 * (H - 1)).round().astype(int)
    for x in range(min(W, len(ys))):
        y0 = ys[x - 1] if x else ys[x]
        a, b = sorted((int(y0), int(ys[x])))
        grid[a:b + 1, x] = True
    mid = H // 2
    lines: list[Text] = []
    for r in range(h):
        t = Text()
        for c in range(w):
            bits = 0
            for side, table in ((0, _LEFT_DOTS), (1, _RIGHT_DOTS)):
                for j in range(4):
                    if grid[r * 4 + j, c * 2 + side]:
                        bits |= table[j]
            if bits:
                amp = abs(((r * 4 + 2) - mid) / max(1, mid))
                t.append(chr(0x2800 + bits), style=color_at(scheme, pal, c / max(1, w - 1), amp))
            else:
                t.append(" ")
        lines.append(t)
    return lines


def draw_vu(levels, w: int, h: int, scheme: str, pal: dict) -> list[Text]:
    """Two stereo level meters with peak hold."""
    l, r, pl, pr = levels
    width = max(4, w - 3)
    lines = [_blank(w) for _ in range(h)]
    rows = [(max(0, h // 2 - 1), "L", l, pl), (min(h - 1, h // 2 + 1), "R", r, pr)] if h >= 3 else [(0, "L", l, pl)]
    for row, label, v, pk in rows:
        t = Text(label + " ", style=pal["muted"])
        cells = v * width
        full = int(cells)
        peak_at = min(width - 1, int(pk * width))
        for i in range(width):
            x = i / max(1, width - 1)
            if i < full:
                t.append("█", style=color_at(scheme, pal, x, x))
            elif i == full and cells - full > 0.5:
                t.append("▌", style=color_at(scheme, pal, x, x))
            elif i == peak_at and pk > 0.05:
                t.append("▎", style=color_at(scheme, pal, x, x))
            else:
                t.append("·", style=pal["muted"])
        t.append(" " * max(0, w - len(t.plain)))
        lines[row] = t
    return lines
