"""Thin asyncio wrapper around an mpv process controlled over its JSON IPC socket."""
from __future__ import annotations

import asyncio
import json
import os
import shutil
from pathlib import Path
from typing import Callable

MPRIS = "/usr/lib/mpv-mpris/mpris.so"
PROPS = ["time-pos", "duration", "pause", "volume", "mute"]


class PlayerError(Exception):
    pass


class Player:
    """on_event(kind, name, value): kind is 'prop' or 'end' (name = reason)."""

    def __init__(self, on_event: Callable[[str, str, object], None], volume: int = 70) -> None:
        self.on_event = on_event
        self.volume = volume
        self.proc: asyncio.subprocess.Process | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._task: asyncio.Task | None = None
        runtime = os.environ.get("XDG_RUNTIME_DIR") or "/tmp"
        self.sock = Path(runtime) / f"ytplay-{os.getpid()}.sock"

    async def start(self) -> None:
        if not shutil.which("mpv"):
            raise PlayerError("mpv is not installed")
        args = [
            "mpv", "--idle=yes", "--no-video", "--no-terminal", "--force-window=no",
            "--ytdl-format=bestaudio/best", "--audio-display=no", "--cache=yes",
            "--demuxer-max-bytes=50MiB", f"--volume={self.volume}",
            "--force-media-title=ytplay", f"--input-ipc-server={self.sock}",
        ]
        if Path(MPRIS).exists():  # enables media keys / playerctl
            args.append(f"--script={MPRIS}")
        self.proc = await asyncio.create_subprocess_exec(
            *args, stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
        )
        reader = None
        for _ in range(60):
            try:
                reader, self._writer = await asyncio.open_unix_connection(str(self.sock))
                break
            except (FileNotFoundError, ConnectionRefusedError):
                await asyncio.sleep(0.1)
        if reader is None:
            raise PlayerError("could not connect to mpv")
        self._task = asyncio.create_task(self._read(reader))
        for i, prop in enumerate(PROPS, 1):
            await self.cmd("observe_property", i, prop)

    async def _read(self, reader: asyncio.StreamReader) -> None:
        while line := await reader.readline():
            try:
                ev = json.loads(line)
            except ValueError:
                continue
            kind = ev.get("event")
            if kind == "property-change":
                self.on_event("prop", ev["name"], ev.get("data"))
            elif kind == "end-file":
                self.on_event("end", ev.get("reason", ""), ev.get("file_error"))

    async def cmd(self, *args) -> None:
        if self._writer and not self._writer.is_closing():
            self._writer.write(json.dumps({"command": list(args)}).encode() + b"\n")
            await self._writer.drain()

    async def play(self, url: str) -> None:
        await self.cmd("loadfile", url, "replace")
        await self.cmd("set_property", "pause", False)

    async def stop(self) -> None:
        await self.cmd("stop")

    async def close(self) -> None:
        if self._task:
            self._task.cancel()
        if self._writer:
            self._writer.close()
        if self.proc and self.proc.returncode is None:
            self.proc.terminate()
            try:
                await asyncio.wait_for(self.proc.wait(), 2)
            except asyncio.TimeoutError:
                self.proc.kill()
        self.sock.unlink(missing_ok=True)
