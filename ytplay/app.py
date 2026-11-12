"""ytplay — a friendly YouTube-playlist music player for the terminal."""
from __future__ import annotations

import asyncio
import random
import shutil
import signal
import sys
import tomllib
from pathlib import Path

from PIL import Image as PILImage, ImageDraw
from rich.table import Table
from rich.text import Text
from textual import events, on
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.message import Message
from textual.screen import ModalScreen
from textual.theme import Theme
from textual.widgets import Footer, Input, Label, OptionList, Static
from textual.widgets.option_list import Option
from textual_image.widget import Image, get_cell_size

try:  # sixel only has 256 colours: dither them, otherwise gradients band badly
    import textual_image._sixel as _sx

    _qcache: dict = {}

    def _dithered_quantize(image, options):  # noqa: ANN001
        key = (image.size, hash(image.tobytes()))
        if key not in _qcache:
            _qcache.clear()
            pal = image.quantize(colors=options.colors, method=PILImage.Quantize.MEDIANCUT)
            _qcache[key] = image.quantize(palette=pal, dither=PILImage.Dither.FLOYDSTEINBERG)
        return _qcache[key]

    _sx._quantize_rgb_image = _dithered_quantize
except Exception:  # library layout changed: fall back to its default quality
    pass

from .control import Control, send as ctl_send
from .player import Player, PlayerError
from .youtube import (MUSIC_ROOT, Downloader, clean, is_local_path, make_zip, offline_index, safe_name, Store, Track, YtError, fetch_playlist, get_thumbnail, search)

# --------------------------------------------------------------------------- theme

DEFAULT_PALETTE = {
    "mode": "dark", "accent": "#89b4fa", "selection": "#45475a", "muted": "#585b70",
    "background": "#1e1e2e", "dark_background": "#181825", "darker_background": "#11111b",
    "lighter_background": "#313244", "foreground": "#cdd6f4", "dark_foreground": "#7f849c",
    "red": "#f38ba8", "yellow": "#f9e2af", "green": "#a6e3a1", "magenta": "#f5c2e7",
}


def load_palette() -> dict:
    pal = dict(DEFAULT_PALETTE)
    try:  # follow the current Omarchy theme
        path = Path.home() / ".local/state/omarchy/current/theme/colors.toml"
        pal.update(tomllib.loads(path.read_text()))
    except (OSError, ValueError):
        pass
    return pal


P = load_palette()


def fmt_time(sec: float | int | None) -> str:
    if not sec or sec < 0:
        return "0:00"
    sec = int(sec)
    h, rem = divmod(sec, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def placeholder_art(size: int = 600) -> PILImage.Image:
    """A little vinyl record for when there is no cover yet."""
    img = PILImage.new("RGB", (size, size), P["dark_background"])
    d = ImageDraw.Draw(img)
    c = size // 2
    for r, col in ((c - 8, P["lighter_background"]), (c - 24, P["dark_background"]),
                   (c - 40, P["lighter_background"]), (c - 56, P["dark_background"]),
                   (c - 72, P["lighter_background"]), (size // 6, P["accent"]),
                   (size // 24, P["dark_background"])):
        d.ellipse((c - r, c - r, c + r, c + r), fill=col)
    return img


# --------------------------------------------------------------------------- widgets


class Slider(Static):
    """A clickable horizontal bar: `left ━━━●──── right`."""

    ALLOW_SELECT = False

    class Changed(Message):
        def __init__(self, slider: "Slider", fraction: float, final: bool) -> None:
            super().__init__()
            self.slider, self.fraction, self.final = slider, fraction, final

    def __init__(self, **kw) -> None:
        super().__init__(**kw)
        self.left, self.right, self.fraction = "", "", 0.0
        self.dragging = False

    def set(self, fraction: float, left: str = "", right: str = "") -> None:
        if self.dragging:  # don't fight the user's pointer
            self.left, self.right = left, right
            return
        self.fraction, self.left, self.right = max(0.0, min(1.0, fraction)), left, right
        self.refresh()

    def _bar_width(self) -> int:
        return max(4, self.size.width - len(self.left) - len(self.right) - 2)

    def render(self) -> Text:
        n = self._bar_width()
        filled = int(round(self.fraction * (n - 1)))
        t = Text()
        t.append(self.left + " ", style=P["dark_foreground"])
        t.append("━" * filled, style=f"bold {P['accent']}")
        t.append("●", style=f"bold {P['foreground']}")
        t.append("─" * (n - filled - 1), style=P["muted"])
        t.append(" " + self.right, style=P["dark_foreground"])
        return t

    def _frac(self, x: float) -> float:
        return max(0.0, min(1.0, (x - len(self.left) - 1) / max(1, self._bar_width() - 1)))

    def on_mouse_down(self, event: events.MouseDown) -> None:
        self.dragging = True
        self.capture_mouse()
        self.fraction = self._frac(event.x)
        self.refresh()
        self.post_message(self.Changed(self, self.fraction, False))
        event.stop()

    def on_mouse_move(self, event: events.MouseMove) -> None:
        if self.dragging:
            self.fraction = self._frac(event.x)
            self.refresh()
            self.post_message(self.Changed(self, self.fraction, False))
            event.stop()

    def on_mouse_up(self, event: events.MouseUp) -> None:
        if self.dragging:
            self.dragging = False
            self.release_mouse()
            self.fraction = self._frac(event.x)
            self.refresh()
            self.post_message(self.Changed(self, self.fraction, True))
            event.stop()


class NowPanel(Vertical):
    def on_resize(self) -> None:
        self.app.call_after_refresh(self.app.fit_art)


class Ctl(Static):
    """A clickable chip that runs an app action."""

    ALLOW_SELECT = False

    def __init__(self, label: str, action: str, **kw) -> None:
        super().__init__(label, **kw)
        self.act = action

    def on_click(self) -> None:
        self.app.run_action(self.act)


class FilterInput(Input):
    class Close(Message):
        def __init__(self, clear: bool) -> None:
            super().__init__()
            self.clear = clear

    BINDINGS = [Binding("escape", "close", show=False)]

    def action_close(self) -> None:
        self.post_message(self.Close(True))

    def _on_input_submitted(self, event) -> None:  # noqa: ANN001
        event.stop()
        self.post_message(self.Close(False))


class Prompt(ModalScreen[str | None]):
    BINDINGS = [Binding("escape", "dismiss_none", show=False)]

    def __init__(self, title: str, placeholder: str, hint: str = "") -> None:
        super().__init__()
        self.title_text, self.placeholder, self.hint = title, placeholder, hint

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog"):
            yield Label(self.title_text, id="dlg-title")
            yield Input(placeholder=self.placeholder)
            yield Label(self.hint + ("   " if self.hint else "") + "enter ✓   esc ✗", id="dlg-hint")

    def on_input_submitted(self, e: Input.Submitted) -> None:
        self.dismiss(e.value.strip() or None)

    def action_dismiss_none(self) -> None:
        self.dismiss(None)


class Confirm(ModalScreen[bool]):
    BINDINGS = [Binding("y,enter", "yes", show=False), Binding("n,escape", "no", show=False)]

    def __init__(self, text: str) -> None:
        super().__init__()
        self.text = text

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog"):
            yield Label(self.text, id="dlg-title")
            yield Label("y / enter  yes     n / esc  no", id="dlg-hint")

    def action_yes(self) -> None:
        self.dismiss(True)

    def action_no(self) -> None:
        self.dismiss(False)


class Choice(ModalScreen[str | None]):
    BINDINGS = [Binding("f", "pick('folder')", show=False), Binding("z", "pick('zip')", show=False),
                Binding("escape", "pick('')", show=False)]

    def __init__(self, title: str, lines: list[str]) -> None:
        super().__init__()
        self.title_text, self.lines = title, lines

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog"):
            yield Label(self.title_text, id="dlg-title")
            for l in self.lines:
                yield Label(l)
            yield Label("esc  cancel", id="dlg-hint")

    def action_pick(self, what: str) -> None:
        self.dismiss(what or None)


HELP = """\
[b]Playback[/b]
  space        play / pause
  n  p         next / previous track
  ← →          seek 5s   (shift: 30s)
  + -          volume      m  mute
  s  r         shuffle / repeat (off → all → one)

[b]Library[/b]
  a            add a YouTube playlist URL, or a local music folder
  g            search YouTube
  /            filter the track list
  D            download this list (folder or zip)    x  cancel download
  u            refresh playlist
  d            remove the highlighted playlist or search
  tab          switch between playlists and tracks
  enter        open playlist / play track

You can also click the progress bar, volume bar and buttons.
Start with a URL:  [b]ytplay <playlist-url>[/b]
"""


class Help(ModalScreen[None]):
    BINDINGS = [Binding("escape,q,question_mark,enter", "dismiss_none", show=False)]

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog"):
            yield Label("ytplay — keys", id="dlg-title")
            yield Static(HELP)
            yield Label("esc to close", id="dlg-hint")

    def action_dismiss_none(self) -> None:
        self.dismiss(None)


# --------------------------------------------------------------------------- app

CSS = """

Screen { background: $yp-bg; color: $yp-fg; }
#body { height: 1fr; padding: 0 1; }
.panel { border: round $yp-muted; background: $yp-panel; border-title-color: $yp-dim;
          border-subtitle-color: $yp-dim; margin: 0 0; }
.panel:focus-within { border: round $yp-accent; border-title-color: $yp-accent; }
#sidebar { width: 28; }
#library { width: 1fr; }
#now { width: 30%; min-width: 40; max-width: 64; padding: 1 2; align-horizontal: center; }
OptionList { background: transparent; border: none; padding: 0 1; height: 1fr; }
OptionList:focus { border: none; }
OptionList > .option-list--option { padding: 0 1; }
OptionList > .option-list--option-highlighted { background: $yp-card; color: $yp-fg; text-style: bold; }
OptionList > .option-list--option-hover { background: $yp-card; }
OptionList:focus > .option-list--option-highlighted { background: $yp-sel; color: $yp-fg; }
#logo { height: 1; padding: 0 2; color: $yp-accent; text-style: bold; margin-bottom: 1; }
#filter { display: none; height: 3; margin: 0 1; border: round $yp-muted; background: $yp-bg; }
#filter:focus { border: round $yp-accent; }
#filter.-on { display: block; }
#empty { padding: 2 3; color: $yp-dim; display: none; }
#art { margin-bottom: 1; }
#np-title { width: 100%; text-style: bold; text-align: center; }
#np-artist { width: 100%; color: $yp-dim; text-align: center; margin-bottom: 1; }
Slider { height: 1; width: 100%; }
#controls { height: 1; width: 100%; margin-top: 1; align-horizontal: center; }
#toggles { height: 1; width: 100%; margin-top: 1; align-horizontal: center; }
.chip { width: auto; height: 1; padding: 0 2; margin: 0 1; background: $yp-card; color: $yp-fg; }
.chip:hover { background: $yp-sel; }
.chip.primary { background: $yp-accent; color: $yp-bg; text-style: bold; min-width: 10; text-align: center; }
.chip.primary:hover { background: $yp-fg; }
#toggles .chip { color: $yp-dim; }
#toggles .chip.-on { color: $yp-accent; background: $yp-sel; text-style: bold; }
#volume { margin-top: 1; }
Footer { background: $yp-bg; }
Footer .footer-key--key { color: $yp-accent; background: $yp-bg; }
Footer .footer-key--description { color: $yp-dim; background: $yp-bg; }
#dialog { width: 64; height: auto; background: $yp-panel; border: round $yp-accent; padding: 1 2;
           align-horizontal: center; }
Prompt, Confirm, Help { align: center middle; background: $yp-bg 70%; }
#dlg-title { text-style: bold; color: $yp-accent; margin-bottom: 1; width: 100%; }
#dlg-hint { color: $yp-dim; margin-top: 1; width: 100%; }
Input { background: $yp-bg; border: tall $yp-muted; }
Input:focus { border: tall $yp-accent; }
"""


class YTPlay(App):
    ALLOW_SELECT = False  # it's a player, not a document: clicks must never select text
    CSS = CSS
    TITLE = "ytplay"
    BINDINGS = [
        Binding("space", "toggle", "Play/Pause"),
        Binding("n", "next", "Next"),
        Binding("p", "prev", "Prev"),
        Binding("left", "seek(-5)", show=False),
        Binding("right", "seek(5)", show=False),
        Binding("shift+left", "seek(-30)", show=False),
        Binding("shift+right", "seek(30)", show=False),
        Binding("plus,equals_sign", "volume(5)", show=False),
        Binding("minus", "volume(-5)", show=False),
        Binding("m", "mute", show=False),
        Binding("s", "shuffle", "Shuffle"),
        Binding("r", "repeat", "Repeat"),
        Binding("a", "add", "Add playlist"),
        Binding("g", "search", "Search"),
        Binding("slash", "filter", "Filter"),
        Binding("u", "refresh", "Refresh", show=False),
        Binding("d", "remove", "Remove"),
        Binding("D", "download", "Download"),
        Binding("x", "cancel_download", show=False),
        Binding("question_mark", "help", "Help"),
        Binding("q", "quit", "Quit"),
    ]

    def get_css_variables(self) -> dict[str, str]:
        v = super().get_css_variables()
        v.update({
            "yp-bg": P["background"], "yp-panel": P["dark_background"], "yp-sel": P["selection"],
            "yp-muted": P["muted"], "yp-accent": P["accent"], "yp-fg": P["foreground"],
            "yp-dim": P["dark_foreground"], "yp-card": P["lighter_background"],
        })
        return v

    def make_theme(self, name: str) -> Theme:
        return Theme(
            name=name, primary=P["accent"], secondary=P["magenta"], accent=P["accent"],
            warning=P["yellow"], error=P["red"], success=P["green"], foreground=P["foreground"],
            background=P["background"], surface=P["dark_background"], panel=P["lighter_background"],
            dark=P.get("mode", "dark") != "light",
        )

    def check_theme(self) -> None:
        """Follow Omarchy theme switches while running (read-only: we never write to the system)."""
        new = load_palette()
        if new == P:
            return
        P.clear()
        P.update(new)
        self._theme_n = getattr(self, "_theme_n", 0) + 1
        name = f"omarchy-{self._theme_n}"
        self.register_theme(self.make_theme(name))
        self.theme = name
        self.refresh_css()
        # re-render everything that bakes colours into Rich text
        ol = self.query_one("#tracks", OptionList)
        for i in range(len(self.view)):
            ol.replace_option_prompt_at_index(i, self.row(i))
        self.refresh_sidebar()
        self.update_now_ui()
        if self.current is None:
            self.set_art(None)
        for sl in self.query(Slider):
            sl.refresh()

    def __init__(self, start_url: str | None = None) -> None:
        super().__init__()
        self.start_url = start_url
        self.store = Store()
        self.player = Player(self._player_event, self.store.settings["volume"])
        # library view
        self.all_tracks: list[Track] = []   # tracks of the opened list (unfiltered)
        self.view: list[Track] = []         # after filtering
        self.sidebar_ids: list[str] = []    # option ids; "pl:<i>" or "search"
        self.search_results: tuple[str, list[Track]] | None = None
        self.opened: str | None = None      # id of the opened sidebar entry
        # playback
        self.queue: list[Track] = []
        self.order: list[int] = []
        self.pos = -1
        self.current: Track | None = None
        self.position = 0.0
        self.duration = 0.0
        self.paused = True
        self.loading = False
        self.volume = float(self.store.settings["volume"])
        self.muted = False
        self.shuffle = bool(self.store.settings["shuffle"])
        self.repeat = self.store.settings["repeat"]
        self._shown_sec = -1
        self.art_src = None
        self.errors_in_a_row = 0
        self.cover_rev = 0
        self.control = Control(self.remote_command)
        self.offline: dict[str, Path] = {}
        self.dl_status = ""
        self.lib_title = ""
        self.downloader: Downloader | None = None

    # ----------------------------------------------------------------- layout

    def compose(self) -> ComposeResult:
        with Horizontal(id="body"):
            with Vertical(id="sidebar", classes="panel"):
                yield Static("ytplay", id="logo")
                yield OptionList(id="playlists")
            with Vertical(id="library", classes="panel"):
                yield FilterInput(placeholder="filter tracks…", id="filter")
                yield OptionList(id="tracks")
                yield Static("", id="empty")
            with NowPanel(id="now", classes="panel"):
                yield Image(placeholder_art(), id="art")
                yield Static("Nothing playing", id="np-title")
                yield Static("pick a track and press enter", id="np-artist")
                yield Slider(id="progress")
                with Horizontal(id="controls"):
                    yield Ctl("prev", "prev", classes="chip")
                    yield Ctl("play", "toggle", id="play", classes="chip primary")
                    yield Ctl("next", "next", classes="chip")
                with Horizontal(id="toggles"):
                    yield Ctl("shuffle", "shuffle", id="shuffle", classes="chip")
                    yield Ctl("repeat", "repeat", id="repeat", classes="chip")
                yield Slider(id="volume")
        yield Footer()

    async def on_mount(self) -> None:
        for sig in (signal.SIGTERM, signal.SIGHUP):  # closing the terminal must clean up, not strand state
            try:
                asyncio.get_running_loop().add_signal_handler(sig, self.exit)
            except (NotImplementedError, RuntimeError):
                pass
        self.register_theme(self.make_theme("omarchy"))
        self.theme = "omarchy"
        self.set_interval(1.5, self.check_theme)
        self.query_one("#sidebar").border_title = "playlists"
        self.query_one("#now").border_title = "now playing"
        self.query_one("#art").styles.visibility = "visible"
        self.offline = offline_index()
        try:
            await self.control.start()
            self.set_interval(4, self.control.heartbeat)
        except OSError as e:
            self.notify(f"Remote control unavailable: {e}", severity="warning")
        self.refresh_sidebar()
        self.update_now_ui()
        try:
            await self.player.start()
        except PlayerError as e:
            self.notify(str(e), severity="error", timeout=10)
        if self.start_url:
            self.run_worker(self.add_playlist(self.start_url), exclusive=False)
        elif self.store.playlists:
            self.open_entry("pl:0")
        else:
            self.show_empty()
        self.query_one("#playlists").focus()

    async def on_unmount(self) -> None:
        self.store.settings.update(volume=int(self.volume), shuffle=self.shuffle, repeat=self.repeat)
        self.store.save()
        await self.control.close()
        await self.player.close()

    def on_resize(self) -> None:
        self.call_after_refresh(self.fit_art)

    def fit_art(self) -> None:
        """Largest square (in pixels, whatever the font's cell shape) that fits the panel."""
        now = self.query_one("#now")
        w, avail = now.content_size.width, now.content_size.height - 13
        if w < 6:
            return
        try:
            cell = get_cell_size()
        except Exception:
            cell = type("C", (), {"width": 10, "height": 20})()
        ratio = cell.width / cell.height
        h = max(4, min(round(w * ratio), avail))
        w = max(8, min(w, round(h / ratio)))
        art = self.query_one("#art")
        art.styles.width, art.styles.height = w, h
        self._art_px = (round(w * cell.width), round(h * cell.height))
        self.render_art()

    def set_art(self, img: PILImage.Image | None) -> None:
        self.art_src = img or placeholder_art()
        if img is not None:
            self.cover_rev += 1
            self.control.save_cover(img, self.cover_rev)
            self.publish_status()
        self.render_art()

    def render_art(self) -> None:
        """Hand the widget an image already at its final pixel size (Lanczos), so nothing is re-scaled coarsely."""
        src = self.art_src or placeholder_art()
        px = getattr(self, "_art_px", None)
        art = self.query_one("#art", Image)
        if src is None:
            return
        art.image = src.resize(px, PILImage.LANCZOS) if px and px[0] > 0 and px[1] > 0 else src

    # ----------------------------------------------------------------- sidebar / library

    def refresh_sidebar(self) -> None:
        ol = self.query_one("#playlists", OptionList)
        ol.clear_options()
        self.sidebar_ids = []
        for i, p in enumerate(self.store.playlists):
            ol.add_option(Option(Text(clean(p["name"])), id=f"pl:{i}"))
            self.sidebar_ids.append(f"pl:{i}")
        if self.search_results:
            ol.add_option(Option(Text.assemble(("search ", P["dark_foreground"]), self.search_results[0]), id="search"))
            self.sidebar_ids.append("search")
        if self.opened in self.sidebar_ids:
            ol.highlighted = self.sidebar_ids.index(self.opened)

    def show_empty(self, text: str | None = None) -> None:
        empty = self.query_one("#empty", Static)
        tracks = self.query_one("#tracks")
        if text is None and self.store.playlists:
            empty.display = False
            tracks.display = True
            return
        empty.update(text or (
            "[b]Welcome to ytplay[/b]\n\nNo playlists yet.\n\n"
            "Press [b]a[/b] and paste a YouTube playlist URL,\n"
            "or press [b]g[/b] to search YouTube."))
        empty.display = True
        tracks.display = False

    @on(OptionList.OptionSelected, "#playlists")
    def _playlist_selected(self, e: OptionList.OptionSelected) -> None:
        self.open_entry(e.option.id)
        self.query_one("#tracks").focus()

    def open_entry(self, entry_id: str | None) -> None:
        if not entry_id:
            return
        self.opened = entry_id
        self.query_one("#filter", FilterInput).value = ""
        if entry_id == "search" and self.search_results:
            self.set_tracks(self.search_results[0], self.search_results[1])
            return
        idx = int(entry_id.split(":")[1])
        pl = self.store.playlists[idx]
        cached = self.store.load_cached(pl["url"])
        if cached:
            self.set_tracks(pl["name"], cached[0])
            if self.store.needs_refresh(pl["url"], cached[1]):
                self.run_worker(self.reload_playlist(idx, quiet=True), exclusive=False)
        else:
            self.set_tracks(pl["name"], [])
            self.show_empty("Loading playlist…")
            self.run_worker(self.reload_playlist(idx), exclusive=False)

    def set_tracks(self, title: str, tracks: list[Track]) -> None:
        self.all_tracks = tracks
        self.lib_title = clean(title)
        self.refresh_lib_title()
        self.apply_filter()

    def refresh_lib_title(self) -> None:
        extra = f"   downloading {self.dl_status}" if self.dl_status else ""
        self.query_one("#library").border_title = self.lib_title + extra

    def apply_filter(self) -> None:
        q = self.query_one("#filter", FilterInput).value.strip().lower()
        self.view = [t for t in self.all_tracks if q in t.title.lower() or q in t.channel.lower()] if q \
            else list(self.all_tracks)
        total = sum(t.duration for t in self.view)
        sub = f"{len(self.view)} tracks · {fmt_time(total)}" if self.view else ""
        n_off = sum(1 for t in self.view if t.id in self.offline or t.is_local_file)
        if sub and n_off:
            sub += f" · {n_off} offline"
        self.query_one("#library").border_subtitle = sub
        ol = self.query_one("#tracks", OptionList)
        ol.clear_options()
        ol.add_options([Option(self.row(i), id=str(i)) for i in range(len(self.view))])
        if self.view:
            ol.highlighted = 0
            self.show_empty()
            self.query_one("#empty").display = False
            ol.display = True
        elif self.all_tracks:
            self.show_empty("No tracks match your filter.")
        elif self.opened and self.store.playlists:
            pass

    def row(self, i: int) -> Table:
        t = self.view[i]
        playing = self.current is not None and t.id == self.current.id
        g = Table.grid(expand=True, padding=(0, 1))
        g.add_column(width=3, justify="right", no_wrap=True)
        g.add_column(ratio=5, no_wrap=True, overflow="ellipsis")
        g.add_column(ratio=2, no_wrap=True, overflow="ellipsis")
        g.add_column(width=7, justify="right", no_wrap=True)
        if playing:
            acc = f"bold {P['accent']}"
            g.add_row(Text(">" if not self.paused else "=", style=acc), Text(t.title, style=acc),
                      Text(t.channel, style=P["accent"]), Text(fmt_time(t.duration), style=P["accent"]))
        else:
            dim = P["dark_foreground"]
            g.add_row(Text(str(i + 1), style=dim), Text(t.title), Text(t.channel, style=dim),
                      Text(fmt_time(t.duration), style=dim))
        return g

    def refresh_rows(self) -> None:
        """Re-render rows so the 'playing' marker follows the current track."""
        ol = self.query_one("#tracks", OptionList)
        for i, t in enumerate(self.view):
            is_cur = self.current is not None and t.id == self.current.id
            if is_cur or getattr(self, "_marked", None) == t.id:
                ol.replace_option_prompt_at_index(i, self.row(i))
        self._marked = self.current.id if self.current else None

    @on(FilterInput.Changed, "#filter")
    def _filter_changed(self) -> None:
        self.apply_filter()

    @on(FilterInput.Close)
    def _filter_close(self, e: FilterInput.Close) -> None:
        f = self.query_one("#filter", FilterInput)
        if e.clear:
            f.value = ""
            f.remove_class("-on")
        self.query_one("#tracks").focus()

    def action_filter(self) -> None:
        f = self.query_one("#filter", FilterInput)
        f.add_class("-on")
        f.focus()

    # ----------------------------------------------------------------- modals (hide the image so it can't paint over them)

    def ask(self, screen: ModalScreen, callback) -> None:  # noqa: ANN001
        art = self.query_one("#art")
        art.styles.visibility = "hidden"

        def done(result) -> None:  # noqa: ANN001
            art.styles.visibility = "visible"
            callback(result)

        self.push_screen(screen, done)

    # ----------------------------------------------------------------- library actions

    def action_add(self) -> None:
        def got(url: str | None) -> None:
            if url:
                self.run_worker(self.add_playlist(url), exclusive=False)
        self.ask(Prompt("Add a playlist or folder", "https://www.youtube.com/playlist?list=…",
                        "a playlist / album / video URL, or a folder like ~/Music"), got)

    async def add_playlist(self, url: str) -> None:
        if url.startswith("file://"):
            url = url[7:]
        if is_local_path(url):
            url = str(Path(url).expanduser())
        elif not url.startswith(("http://", "https://")):
            self.notify("That's not a URL or a folder. To look for songs, press g.", severity="warning")
            return
        self.notify("Fetching playlist…", timeout=3)
        try:
            title, tracks = await fetch_playlist(url)
        except YtError as e:
            self.notify(str(e), title="Couldn't add playlist", severity="error", timeout=8)
            return
        idx = self.store.add(title, url)
        self.store.save_cached(url, tracks)
        self.refresh_sidebar()
        self.open_entry(f"pl:{idx}")
        self.notify(f"{title} — {len(tracks)} tracks", title="Added")

    async def reload_playlist(self, idx: int, quiet: bool = False) -> None:
        pl = self.store.playlists[idx]
        if not quiet:
            self.notify("Refreshing…", timeout=2)
        try:
            title, tracks = await fetch_playlist(pl["url"])
        except YtError as e:
            self.show_empty() if self.all_tracks else self.show_empty(f"Couldn't load this playlist.\n\n{e}")
            if not quiet:
                self.notify(str(e), title="Refresh failed", severity="error")
            return
        self.store.save_cached(pl["url"], tracks)
        pl["name"] = title
        self.store.save()
        if self.opened == f"pl:{idx}":
            self.set_tracks(title, tracks)
            self.refresh_sidebar()
        if not quiet:
            self.notify(f"{len(tracks)} tracks", title="Up to date")

    def action_refresh(self) -> None:
        if self.opened and self.opened.startswith("pl:"):
            self.run_worker(self.reload_playlist(int(self.opened[3:])), exclusive=False)

    def action_remove(self) -> None:
        entry = self.opened
        side = self.query_one("#playlists", OptionList)
        if self.focused is side and side.highlighted is not None:
            entry = self.sidebar_ids[side.highlighted]
        if not entry:
            return

        def reopen() -> None:
            self.opened = None
            self.refresh_sidebar()
            if self.store.playlists:
                self.open_entry("pl:0")
            else:
                self.set_tracks("", [])
                self.show_empty()

        if entry == "search":
            self.search_results = None
            reopen()
            return
        idx = int(entry[3:])
        name = clean(self.store.playlists[idx]["name"])

        def got(yes: bool | None) -> None:
            if yes:
                self.store.remove(idx)
                reopen()

        self.ask(Confirm(f"Remove “{name}” from your library?"), got)

    def action_download(self) -> None:
        tracks = [t for t in self.all_tracks if not t.is_local_file]
        if not tracks:
            self.notify("Nothing to download here (local files are already on disk).", severity="warning")
            return
        if self.dl_status:
            self.notify("A download is already running. Press x to cancel it.", severity="warning")
            return
        name = self.lib_title or "playlist"
        dest = MUSIC_ROOT / safe_name(name)

        def got(choice: str | None) -> None:
            if choice:
                self.run_worker(self.do_download(name, tracks, choice == "zip"), group="dl", exclusive=False)

        self.ask(Choice(f"Download “{name}” ({len(tracks)} tracks)", [
            f"f   folder   {dest}/",
            f"z   zip      {dest}.zip",
            "",
            "Audio is saved as m4a with cover art and tags.",
            "Downloaded tracks play offline automatically.",
        ]), got)

    async def do_download(self, name: str, tracks: list[Track], as_zip: bool) -> None:
        dest = MUSIC_ROOT / safe_name(name)
        existed = dest.exists()
        self.downloader = Downloader()

        def progress(done: int, failed: int, total: int) -> None:
            self.dl_status = f"{done + failed}/{total}"
            self.refresh_lib_title()

        self.dl_status = f"0/{len(tracks)}"
        self.refresh_lib_title()
        self.notify(f"Downloading {len(tracks)} tracks…  (x to cancel)", timeout=4)
        try:
            done, failed = await self.downloader.run(tracks, dest, progress)
            if as_zip:
                self.dl_status = "zipping"
                self.refresh_lib_title()
                zip_path = dest.with_suffix(".zip")
                await asyncio.to_thread(make_zip, dest, zip_path)
                if not existed:
                    await asyncio.to_thread(shutil.rmtree, dest, True)
                where = str(zip_path)
            else:
                where = str(dest)
            msg = f"{done} saved" + (f", {failed} failed" if failed else "")
            self.notify(f"{msg}\n{where}", title="Download finished", timeout=12,
                        severity="warning" if failed else "information")
        except asyncio.CancelledError:
            self.notify("Download cancelled", severity="warning")
            raise
        finally:
            self.dl_status = ""
            self.downloader = None
            self.refresh_lib_title()
            self.offline = await asyncio.to_thread(offline_index)
            self.apply_filter_keep()

    def apply_filter_keep(self) -> None:
        """Refresh the subtitle (offline count) without losing the cursor."""
        ol = self.query_one("#tracks", OptionList)
        hl = ol.highlighted
        self.apply_filter()
        if hl is not None and self.view:
            ol.highlighted = min(hl, len(self.view) - 1)

    def action_cancel_download(self) -> None:
        if self.dl_status:
            self.workers.cancel_group(self, "dl")

    def action_search(self) -> None:
        def got(q: str | None) -> None:
            if q:
                self.run_worker(self.do_search(q), exclusive=False)
        self.ask(Prompt("Search YouTube", "artist, song, album…"), got)

    async def do_search(self, q: str) -> None:
        self.notify(f"Searching “{q}”…", timeout=3)
        try:
            tracks = await search(q)
        except YtError as e:
            self.notify(str(e), title="Search failed", severity="error")
            return
        self.search_results = (f"“{q}”", tracks)
        self.refresh_sidebar()
        self.open_entry("search")
        self.query_one("#tracks").focus()

    def action_help(self) -> None:
        self.ask(Help(), lambda _: None)

    # ----------------------------------------------------------------- playback

    @on(OptionList.OptionSelected, "#tracks")
    async def _track_selected(self, e: OptionList.OptionSelected) -> None:
        await self.play_queue(self.view, e.option_index)

    def build_order(self, first: int) -> None:
        self.order = list(range(len(self.queue)))
        if self.shuffle:
            rest = [i for i in self.order if i != first]
            random.shuffle(rest)
            self.order = [first] + rest
            self.pos = 0
        else:
            self.pos = first

    async def play_queue(self, tracks: list[Track], start: int) -> None:
        self.queue = list(tracks)
        self.build_order(start)
        await self.play_pos(self.pos)

    async def play_pos(self, pos: int) -> None:
        self.pos = pos
        track = self.queue[self.order[pos]]
        self.current = track
        self.position, self.duration, self.loading, self.paused = 0.0, float(track.duration), True, False
        self._shown_sec = -1
        self.art_src = None
        self.update_now_ui()
        self.refresh_rows()
        self.run_worker(self.load_art(track), exclusive=False)
        local = self.offline.get(track.id)
        await self.player.play(str(local) if local and local.exists() else track.url)

    async def load_art(self, track: Track) -> None:
        img = await get_thumbnail(track.id)
        if self.current is track:
            self.set_art(img)

    async def advance(self, step: int, auto: bool = False) -> None:
        if not self.queue:
            return
        if auto and self.repeat == "one":
            await self.play_pos(self.pos)
            return
        nxt = self.pos + step
        if nxt >= len(self.order):
            if self.repeat == "all" or not auto:
                if self.shuffle:
                    self.build_order(random.randrange(len(self.queue)))
                nxt = 0
            else:
                await self.stop_playback()
                return
        elif nxt < 0:
            nxt = 0
        await self.play_pos(nxt)

    async def stop_playback(self) -> None:
        await self.player.stop()
        self.current = None
        self.paused, self.loading, self.position, self.duration = True, False, 0.0, 0.0
        self.set_art(None)
        self.update_now_ui()
        self.refresh_rows()

    # mpv → app (runs on the app's event loop)
    def _player_event(self, kind: str, name: str, value: object) -> None:
        val = value
        if kind == "prop":
            if name == "time-pos" and value is not None:
                self.position = float(value)
                self.errors_in_a_row = 0
                if self.loading:
                    self.loading = False
                    self.update_now_ui()
                if int(self.position) != self._shown_sec:
                    self._shown_sec = int(self.position)
                    self.update_progress()
            elif name == "duration" and value:
                self.duration = float(value)
                self.update_progress()
            elif name == "pause":
                self.paused = bool(value)
                self.update_now_ui()
                self.refresh_rows()
            elif name == "volume" and value is not None:
                self.volume = float(value)
                self.update_volume()
            elif name == "mute":
                self.muted = bool(value)
                self.update_volume()
        elif kind == "end" and self.current is not None:
            if name == "eof":
                self.run_worker(self.advance(1, auto=True), exclusive=False)
            elif name == "error":
                self.errors_in_a_row += 1
                why = f" ({val})" if val else ""
                if self.errors_in_a_row >= 3:
                    self.notify(f"Playback keeps failing{why}. Stopped.", severity="error", timeout=10)
                    self.errors_in_a_row = 0
                    self.run_worker(self.stop_playback(), exclusive=False)
                else:
                    self.notify(f"Can't play “{self.current.title}”{why} — skipping", severity="warning")
                    self.run_worker(self.advance(1, auto=True), exclusive=False)

    # ----------------------------------------------------------------- actions

    async def action_toggle(self) -> None:
        if self.current is None:
            ol = self.query_one("#tracks", OptionList)
            if self.view:
                await self.play_queue(self.view, ol.highlighted or 0)
            return
        await self.player.cmd("cycle", "pause")

    async def action_next(self) -> None:
        await self.advance(1)

    async def action_prev(self) -> None:
        if self.position > 4 or self.pos <= 0:
            await self.player.cmd("seek", 0, "absolute")
        else:
            await self.advance(-1)

    async def action_seek(self, secs: int) -> None:
        if self.current:
            await self.player.cmd("seek", secs, "relative")

    async def action_volume(self, delta: int) -> None:
        await self.player.cmd("set_property", "volume", max(0, min(130, self.volume + delta)))

    async def action_mute(self) -> None:
        await self.player.cmd("cycle", "mute")

    def action_shuffle(self) -> None:
        self.shuffle = not self.shuffle
        if self.queue:
            cur = self.order[self.pos]
            self.build_order(cur)
        self.update_now_ui()

    def action_repeat(self) -> None:
        self.repeat = {"off": "all", "all": "one", "one": "off"}[self.repeat]
        self.update_now_ui()

    @on(Slider.Changed)
    async def _slider(self, e: Slider.Changed) -> None:
        f = max(0.0, min(1.0, e.fraction))
        if e.slider.id == "progress":
            if e.final and self.current and self.duration:  # seek once, when released
                await self.player.cmd("seek", f * self.duration, "absolute")
        elif e.slider.id == "volume":
            await self.player.cmd("set_property", "volume", round(f * 100))

    # ----------------------------------------------------------------- desktop integration

    def publish_status(self) -> None:
        t = self.current
        self.control.publish({
            "running": True, "active": t is not None,
            "title": t.title if t else "", "artist": t.channel if t else "",
            "playing": bool(t) and not self.paused, "loading": self.loading,
            "position": int(self.position), "duration": int(self.duration),
            "volume": int(self.volume), "muted": self.muted,
            "shuffle": self.shuffle, "repeat": self.repeat, "cover": self.cover_rev,
        })

    async def remote_command(self, parts: list[str]) -> None:
        cmd, args = parts[0], parts[1:]
        num = int(float(args[0])) if args else 0
        match cmd:
            case "toggle" | "playpause":
                await self.action_toggle()
            case "play":
                if self.current is None or self.paused:
                    await self.action_toggle()
            case "pause":
                if self.current is not None and not self.paused:
                    await self.action_toggle()
            case "next":
                await self.action_next()
            case "prev" | "previous":
                await self.action_prev()
            case "prevtrack":  # always the previous track (plain `prev` restarts the current one after 4s)
                if self.queue:
                    await self.advance(-1)
            case "seekto":
                if self.current:
                    await self.player.cmd("seek", max(0, num), "absolute")
            case "volumeto":
                await self.player.cmd("set_property", "volume", max(0, min(130, num)))
            case "seek":
                await self.action_seek(num)
            case "volume":
                await self.action_volume(num)
            case "mute":
                await self.action_mute()
            case "shuffle":
                self.action_shuffle()
            case "repeat":
                self.action_repeat()
            case _:
                raise ValueError(f"unknown command: {cmd}")

    # ----------------------------------------------------------------- now playing UI

    def update_progress(self) -> None:
        frac = self.position / self.duration if self.duration else 0
        right = fmt_time(self.duration) if self.duration else "--:--"
        self.query_one("#progress", Slider).set(frac, fmt_time(self.position), right)
        self.publish_status()

    def update_volume(self) -> None:
        txt = "muted" if self.muted else f"{int(self.volume)}%"
        self.query_one("#volume", Slider).set(self.volume / 100, "vol", txt)
        self.publish_status()

    def update_now_ui(self) -> None:
        t = self.current
        self.query_one("#np-title", Static).update(Text(t.title, overflow="ellipsis", no_wrap=False) if t
                                                   else "Nothing playing")
        sub = ("loading…" if self.loading else t.channel) if t else "pick a track and press enter"
        self.query_one("#np-artist", Static).update(Text(sub, overflow="ellipsis", no_wrap=True))
        self.query_one("#play", Ctl).update("pause" if (t and not self.paused) else "play")
        self.query_one("#shuffle", Ctl).set_class(self.shuffle, "-on")
        rep = self.query_one("#repeat", Ctl)
        rep.update({"off": "repeat", "all": "repeat all", "one": "repeat one"}[self.repeat])
        rep.set_class(self.repeat != "off", "-on")
        self.update_progress()
        self.update_volume()


def main() -> None:
    if sys.argv[1:2] == ["ctl"]:
        sys.exit(ctl_send(sys.argv[2:]))
    url = sys.argv[1] if len(sys.argv) > 1 else None
    if url in ("-h", "--help"):
        print("usage: ytplay [playlist-url]\n       ytplay ctl <toggle|next|prev|seek N|volume N|shuffle|repeat|mute>\n\n"
              "A YouTube playlist music player. Press ? inside for keys.")
        return
    YTPlay(url).run()


if __name__ == "__main__":
    main()
