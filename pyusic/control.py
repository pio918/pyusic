"""Remote control + status for desktop integrations (bar widgets, keybindings).

* status file   $XDG_RUNTIME_DIR/pyusic/status.json   — what is playing, updated live
* cover image   $XDG_RUNTIME_DIR/pyusic/cover-<n>.jpg — current cover (n = status.cover)
* control sock  $XDG_RUNTIME_DIR/pyusic/control.sock   — one command per line

Commands: toggle play pause next prev prevtrack seek <±sec> seekto <sec> volume <±n> volumeto <n>
          mute shuffle repeat
"""
from __future__ import annotations

import asyncio
import json
import os
import socket
import sys
import time
from pathlib import Path
from typing import Awaitable, Callable

from PIL import Image

RUN_DIR = Path(os.environ.get("XDG_RUNTIME_DIR") or f"/tmp/pyusic-{os.getuid()}") / "pyusic"
STATUS = RUN_DIR / "status.json"
SOCK = RUN_DIR / "control.sock"



class Control:
    def __init__(self, handler: Callable[[list[str]], Awaitable[None]]) -> None:
        self.handler = handler
        self.server: asyncio.AbstractServer | None = None
        self._last = ""
        self._current: dict = {"running": False, "active": False}

    async def start(self) -> None:
        RUN_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
        SOCK.unlink(missing_ok=True)
        self.server = await asyncio.start_unix_server(self._client, str(SOCK))
        SOCK.chmod(0o600)
        self.publish({"running": True, "active": False})

    def heartbeat(self) -> None:
        self.publish(self._current, force=True)

    async def _client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            while line := await reader.readline():
                parts = line.decode(errors="replace").split()
                if parts:
                    try:
                        await self.handler(parts)
                        writer.write(b"ok\n")
                    except Exception as e:  # never let a bad command kill the player
                        writer.write(f"error {e}\n".encode())
                    await writer.drain()
        except ConnectionError:
            pass
        finally:
            writer.close()

    def publish(self, status: dict, force: bool = False) -> None:
        """Write the status when it changed (or when `force`, as a heartbeat).

        `ts` lets viewers ignore a status left behind by a player that was killed."""
        self._current = status
        text = json.dumps(status)
        if text == self._last and not force:
            return
        self._last = text
        tmp = STATUS.with_suffix(".tmp")
        try:
            tmp.write_text(json.dumps({**status, "ts": int(time.time())}))
            tmp.replace(STATUS)
        except OSError:
            pass

    def save_cover(self, img: Image.Image, rev: int) -> None:
        """Written as cover-<rev>.jpg: a new name per track, so viewers never show a cached old one."""
        try:
            for old in RUN_DIR.glob("cover-*.jpg"):
                old.unlink(missing_ok=True)
            tmp = RUN_DIR / f"cover-{rev}.tmp"
            img.convert("RGB").resize((160, 160), Image.LANCZOS).save(tmp, "JPEG", quality=88)
            tmp.replace(RUN_DIR / f"cover-{rev}.jpg")
        except OSError:
            pass

    async def close(self) -> None:
        if self.server:
            self.server.close()
        SOCK.unlink(missing_ok=True)
        for old in RUN_DIR.glob("cover-*.jpg"):
            old.unlink(missing_ok=True)
        self.publish({"running": False, "active": False})


def send(args: list[str]) -> int:
    """`pyusic ctl <command…>` — returns a shell exit code."""
    if not args:
        print(__doc__)
        return 2
    try:
        with socket.socket(socket.AF_UNIX) as s:
            s.settimeout(3)
            s.connect(str(SOCK))
            s.sendall((" ".join(args) + "\n").encode())
            reply = s.recv(256).decode().strip()
    except OSError:
        print("pyusic is not running", file=sys.stderr)
        return 1
    if reply != "ok":
        print(reply, file=sys.stderr)
        return 1
    return 0
