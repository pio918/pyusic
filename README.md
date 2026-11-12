# ytplay

A friendly terminal music player for YouTube playlists, with real cover art.

- Paste a YouTube playlist URL (or search YouTube) and play it: audio streams through `mpv`.
- Cover art rendered as an actual image (sixel, kitty graphics, or half-blocks as a fallback).
- Follows your [Omarchy](https://omarchy.org) theme live; sane defaults elsewhere.
- Mouse-friendly: clickable controls, draggable progress and volume bars.
- Download a playlist as a folder of tagged `m4a` files, or as a zip. Downloaded tracks play offline automatically.
- Add any local music folder as a playlist.
- Optional Omarchy bar widget with now-playing, a player popup and wheel/click controls.

Tested on Arch/Omarchy with the `foot` terminal (sixel).

## Install

Requires `python3`, `mpv`, `yt-dlp`, `ffmpeg` (and `socat` for the bar widget's fallback path).

```sh
git clone <this repo> && cd ytplay
./install.sh        # venv + ~/.local/bin/ytplay
ytplay
```

## Keys

| key | action |
|---|---|
| `space` | play / pause |
| `n` `p` | next / previous |
| `←` `→` | seek 5 s (shift: 30 s) |
| `+` `-` `m` | volume / mute |
| `s` `r` | shuffle / repeat (off → all → one) |
| `a` | add a playlist URL or a local folder |
| `g` | search YouTube |
| `/` | filter the track list |
| `D` | download the list (folder or zip), `x` cancels |
| `u` `d` | refresh / remove the highlighted playlist |
| `?` | help |

`ytplay <playlist-url>` adds and opens a playlist straight away.

## Data

- Library: `~/.config/ytplay/library.json`
- Cache: `~/.cache/ytplay/`
- Downloads: `~/Music/ytplay/<playlist>/` (`$XDG_MUSIC_DIR` is respected)

## Remote control

While running, ytplay serves a control socket and a status file under `$XDG_RUNTIME_DIR/ytplay/`.

```sh
ytplay ctl toggle | next | prev | prevtrack | seek 10 | seekto 90 | volume -5 | volumeto 40 | shuffle | repeat | mute
```

Handy for keybindings and bar widgets. `status.json` holds title, artist, position, volume, etc.

## Omarchy bar widget

```sh
./omarchy-widget/install.sh
```

Fixed-width now-playing item next to the clock: cover, title, progress line.
Left click opens a player popup, middle click toggles play/pause, right click opens ytplay, the wheel changes track.
Remove with `omarchy plugin disable pyus.ytplay`.
