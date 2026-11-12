"""YouTube access (via yt-dlp), thumbnails, and on-disk storage."""
from __future__ import annotations

import asyncio
import hashlib
import io
import json
import os
import re
import time
import urllib.request
from dataclasses import asdict, dataclass
from pathlib import Path

from PIL import Image, ImageChops

CONFIG_DIR = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "ytplay"
CACHE_DIR = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")) / "ytplay"


_EMOJI = re.compile(
    "[\U0001F000-\U0001FAFF\u2600-\u27BF\u2300-\u23FF\u2B00-\u2BFF\u2190-\u21FF\u2900-\u297F"
    "\uFE00-\uFE0F\u200D\u20E3\u00A9\u00AE\u203C\u2049\u2122\u2139\u3030\u303D\U000E0000-\U000E007F]"
)


def clean(text: str) -> str:
    """Strip emoji (and their joiners) from titles; keep letters, accents, CJK, punctuation."""
    text = _EMOJI.sub("", text)
    return re.sub(r"\s{2,}", " ", text).strip(" -|\u2022") or text.strip()


class YtError(Exception):
    pass


@dataclass
class Track:
    id: str
    title: str
    channel: str = ""
    duration: int = 0

    @property
    def is_local_file(self) -> bool:
        return self.id.startswith("local:")

    @property
    def url(self) -> str:
        if self.is_local_file:
            return self.id[6:]
        return f"https://www.youtube.com/watch?v={self.id}"


# --------------------------------------------------------------------------- yt-dlp


async def _ytdlp_json(*args: str) -> dict:
    try:
        proc = await asyncio.create_subprocess_exec(
            "yt-dlp", "--no-warnings", "--ignore-errors", "-J", "--flat-playlist", *args,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except FileNotFoundError:
        raise YtError("yt-dlp is not installed") from None
    out, err = await proc.communicate()
    try:
        data = json.loads(out) if out.strip() else None
    except ValueError:
        data = None
    if not isinstance(data, dict):
        lines = [l for l in err.decode(errors="replace").splitlines() if l.strip()]
        raise YtError(lines[-1].removeprefix("ERROR: ") if lines else "yt-dlp returned nothing")
    return data


def _track(e: dict) -> Track | None:
    if not e or not e.get("id"):
        return None
    title = clean((e.get("title") or "").strip())
    if not title or title in ("[Private video]", "[Deleted video]"):
        return None
    channel = (e.get("channel") or e.get("uploader") or "").removesuffix(" - Topic")
    channel = clean(channel)
    return Track(e["id"], title, channel, int(e.get("duration") or 0))


def _parse(data: dict) -> tuple[str, list[Track]]:
    entries = data.get("entries")
    if entries is None:  # a single video URL
        t = _track(data)
        return (data.get("title") or "Video"), ([t] if t else [])
    tracks = [t for t in map(_track, entries) if t]
    return (data.get("title") or "Playlist"), tracks


async def fetch_playlist(url: str) -> tuple[str, list[Track]]:
    if is_local_path(url):
        return await scan_folder(Path(url).expanduser())
    title, tracks = _parse(await _ytdlp_json(url))
    if not tracks:
        raise YtError("No playable videos found")
    return title, tracks


async def search(query: str, n: int = 30) -> list[Track]:
    _, tracks = _parse(await _ytdlp_json(f"ytsearch{n}:{query}"))
    if not tracks:
        raise YtError("No results")
    return tracks


# --------------------------------------------------------------------------- local files

AUDIO_EXT = {".m4a", ".mp3", ".opus", ".ogg", ".oga", ".flac", ".wav", ".aac", ".wma", ".mka", ".webm"}
MUSIC_ROOT = Path(os.environ.get("XDG_MUSIC_DIR") or Path.home() / "Music") / "ytplay"
_ID_RE = re.compile(r"\[([A-Za-z0-9_-]{11})\]\.[A-Za-z0-9]+$")


def is_local_path(s: str) -> bool:
    return s.startswith(("/", "~", "./", "file://")) or Path(s).expanduser().is_dir()


def safe_name(name: str) -> str:
    name = re.sub(r'[\\/:*?"<>|\x00-\x1f]', "", clean(name)).strip(". ")
    return name[:100] or "playlist"


def offline_index() -> dict[str, Path]:
    """YouTube id -> downloaded file, for everything under the download folder."""
    found: dict[str, Path] = {}
    if MUSIC_ROOT.is_dir():
        for f in MUSIC_ROOT.rglob("*"):
            m = _ID_RE.search(f.name)
            if m and f.suffix.lower() in AUDIO_EXT:
                found[m.group(1)] = f
    return found


async def _probe(path: Path, sem: asyncio.Semaphore) -> Track:
    async with sem:
        title, artist, dur = path.stem, "", 0
        try:
            proc = await asyncio.create_subprocess_exec(
                "ffprobe", "-v", "quiet", "-print_format", "json", "-show_format", str(path),
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
            )
            out, _ = await proc.communicate()
            fmt = json.loads(out).get("format", {})
            tags = {k.lower(): v for k, v in fmt.get("tags", {}).items()}
            title = tags.get("title") or title
            artist = tags.get("artist") or tags.get("album_artist") or ""
            dur = int(float(fmt.get("duration") or 0))
        except Exception:
            pass
        m = _ID_RE.search(path.name)
        if not tags_ok(title, path) and m:
            title = re.sub(r"^\d+ - ", "", path.stem[: m.start()].strip())
        return Track(f"local:{path}", clean(title), clean(artist), dur)


def tags_ok(title: str, path: Path) -> bool:
    return title != path.stem


async def scan_folder(folder: Path) -> tuple[str, list[Track]]:
    if not folder.is_dir():
        raise YtError(f"Not a folder: {folder}")
    files = sorted(f for f in folder.rglob("*") if f.suffix.lower() in AUDIO_EXT)
    if not files:
        raise YtError("No audio files found in that folder")
    sem = asyncio.Semaphore(8)
    return folder.name, list(await asyncio.gather(*(_probe(f, sem) for f in files)))


def folder_changed(folder: str, since: float) -> bool:
    try:
        root = Path(folder).expanduser()
        return max(p.stat().st_mtime for p in [root, *root.glob("*")]) > since
    except (OSError, ValueError):
        return False


def _embedded_cover(path: str) -> Image.Image | None:
    import subprocess
    r = subprocess.run(
        ["ffmpeg", "-v", "quiet", "-i", path, "-an", "-frames:v", "1", "-c:v", "mjpeg", "-f", "image2pipe", "-"],
        capture_output=True, timeout=15,
    )
    if not r.stdout:
        return None
    img = Image.open(io.BytesIO(r.stdout)).convert("RGB")
    side = min(img.size)
    left, top = (img.width - side) // 2, (img.height - side) // 2
    return img.crop((left, top, left + side, top + side))


class Downloader:
    """Download tracks as tagged m4a files (cover + metadata embedded), a few at a time."""

    def __init__(self) -> None:
        self.procs: set[asyncio.subprocess.Process] = set()

    async def run(self, tracks: list[Track], dest: Path, progress, jobs: int = 3) -> tuple[int, int]:
        dest.mkdir(parents=True, exist_ok=True)
        have = set(offline_index_for(dest))
        sem = asyncio.Semaphore(jobs)
        state = {"done": 0, "failed": 0}
        todo = [t for t in tracks if not t.is_local_file]

        async def one(i: int, t: Track) -> None:
            async with sem:
                ok = t.id in have
                if not ok:
                    proc = await asyncio.create_subprocess_exec(
                        "yt-dlp", "--no-warnings", "--no-playlist", "-q",
                        "-f", "bestaudio[ext=m4a]/bestaudio", "-x", "--audio-format", "m4a",
                        "--embed-metadata", "--embed-thumbnail", "--convert-thumbnails", "jpg",
                        "-o", str(dest / f"{i + 1:03d} - %(title).120B [%(id)s].%(ext)s"), t.url,
                        stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.DEVNULL,
                        stderr=asyncio.subprocess.DEVNULL,
                    )
                    self.procs.add(proc)
                    try:
                        ok = (await proc.wait()) == 0
                    finally:
                        self.procs.discard(proc)
                state["done" if ok else "failed"] += 1
                progress(state["done"], state["failed"], len(todo))

        try:
            await asyncio.gather(*(one(i, t) for i, t in enumerate(todo)))
        finally:
            for p in list(self.procs):
                if p.returncode is None:
                    p.terminate()
        return state["done"], state["failed"]


def offline_index_for(folder: Path) -> dict[str, Path]:
    return {m.group(1): f for f in folder.glob("*") if (m := _ID_RE.search(f.name))}


def make_zip(folder: Path, zip_path: Path) -> None:
    import zipfile
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_STORED) as z:  # audio is already compressed
        for f in sorted(folder.iterdir()):
            if f.suffix.lower() in AUDIO_EXT:
                z.write(f, f"{folder.name}/{f.name}")


# --------------------------------------------------------------------------- storage


class Store:
    """Saved playlists, settings, and cached track lists."""

    def __init__(self) -> None:
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        (CACHE_DIR / "lists").mkdir(parents=True, exist_ok=True)
        (CACHE_DIR / "thumbs").mkdir(parents=True, exist_ok=True)
        self.file = CONFIG_DIR / "library.json"
        self.playlists: list[dict] = []
        self.settings: dict = {"volume": 70, "shuffle": False, "repeat": "off"}
        try:
            data = json.loads(self.file.read_text())
            self.playlists = data.get("playlists", [])
            self.settings.update(data.get("settings", {}))
        except (OSError, ValueError):
            pass

    def save(self) -> None:
        tmp = self.file.with_suffix(".tmp")
        tmp.write_text(json.dumps({"playlists": self.playlists, "settings": self.settings}, indent=2))
        tmp.replace(self.file)

    def add(self, name: str, url: str) -> int:
        for i, p in enumerate(self.playlists):
            if p["url"] == url:
                p["name"] = name
                self.save()
                return i
        self.playlists.append({"name": name, "url": url})
        self.save()
        return len(self.playlists) - 1

    def remove(self, index: int) -> None:
        p = self.playlists.pop(index)
        self._list_file(p["url"]).unlink(missing_ok=True)
        self.save()

    @staticmethod
    def _list_file(url: str) -> Path:
        return CACHE_DIR / "lists" / (hashlib.sha1(url.encode()).hexdigest() + ".json")

    def load_cached(self, url: str) -> tuple[list[Track], float] | None:
        f = self._list_file(url)
        try:
            tracks = [Track(**t) for t in json.loads(f.read_text())]
            for t in tracks:
                t.title, t.channel = clean(t.title), clean(t.channel)
            return tracks, f.stat().st_mtime
        except (OSError, ValueError, TypeError):
            return None

    def save_cached(self, url: str, tracks: list[Track]) -> None:
        self._list_file(url).write_text(json.dumps([asdict(t) for t in tracks]))

    @staticmethod
    def is_stale(mtime: float, hours: float = 6) -> bool:
        return time.time() - mtime > hours * 3600

    def needs_refresh(self, url: str, mtime: float) -> bool:
        if is_local_path(url):
            return folder_changed(url, mtime)
        return self.is_stale(mtime)


# --------------------------------------------------------------------------- thumbnails


def _download(video_id: str, quality: str) -> bytes | None:
    path = CACHE_DIR / "thumbs" / f"{video_id}.{quality}.jpg"
    if path.exists():
        return path.read_bytes()
    try:
        req = urllib.request.Request(
            f"https://i.ytimg.com/vi/{video_id}/{quality}.jpg", headers={"User-Agent": "Mozilla/5.0"}
        )
        with urllib.request.urlopen(req, timeout=10) as r:
            data = r.read()
    except Exception:
        return None
    if len(data) < 2000:  # YouTube serves a tiny grey placeholder when a size doesn't exist
        return None
    path.write_bytes(data)
    return data


def _fetch_thumb(video_id: str) -> Image.Image:
    """Best available cover as a centred square (up to 720px)."""
    data = _download(video_id, "maxresdefault")
    letterboxed = data is None
    if data is None:
        data = _download(video_id, "hqdefault")
    if data is None:
        raise OSError("no thumbnail")
    img = Image.open(io.BytesIO(data)).convert("RGB")
    if letterboxed:  # hqdefault is 4:3 with black bars; strip them
        bbox = ImageChops.difference(img, Image.new("RGB", img.size, (0, 0, 0))).point(
            lambda v: 255 if v > 12 else 0
        ).getbbox()
        if bbox:
            img = img.crop(bbox)
    side = min(img.size)
    left, top = (img.width - side) // 2, (img.height - side) // 2
    return img.crop((left, top, left + side, top + side))


async def get_thumbnail(video_id: str) -> Image.Image | None:
    try:
        if video_id.startswith("local:"):
            return await asyncio.to_thread(_embedded_cover, video_id[6:])
        return await asyncio.to_thread(_fetch_thumb, video_id)
    except Exception:
        return None
