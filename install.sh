#!/bin/sh
# Sets up a virtualenv and links `ytplay` into ~/.local/bin.
set -e
cd "$(dirname "$(readlink -f "$0")")"
for tool in python3 mpv yt-dlp ffmpeg; do
  command -v "$tool" >/dev/null || { echo "missing: $tool (install it first)"; exit 1; }
done
python3 -m venv .venv
.venv/bin/pip install -q -r requirements.txt
mkdir -p "$HOME/.local/bin"
ln -sf "$PWD/bin/ytplay" "$HOME/.local/bin/ytplay"
echo "done: run 'ytplay'  (make sure ~/.local/bin is in your PATH)"
