# pyusic

A friendly terminal music player for YouTube playlists, with real cover art.

- Paste a YouTube playlist URL (or search YouTube) and play it: audio streams through `mpv`.
- Cover art rendered as an actual image (sixel, kitty graphics, or half-blocks as a fallback).
- Follows your [Omarchy](https://omarchy.org) theme live; sane defaults elsewhere.
- Mouse-friendly: clickable controls, draggable progress and volume bars.
- Download a playlist as a folder of tagged `m4a` files, or as a zip. Downloaded tracks play offline automatically.
- Add any local music folder as a playlist.
- Audio visualizer (spectrum, braille, mirror, oscilloscope, stereo VU) in 5 colour schemes, fed only by pyusic's own audio.
- Optional Omarchy bar widget with now-playing, a player popup and wheel/click controls.

Tested on Arch/Omarchy with the `foot` terminal (sixel).

## Install

Requires `python3`, `mpv`, `yt-dlp`, `ffmpeg` (and `socat` for the bar widget's fallback path).
The visualizer needs PipeWire's `pw-cat` and `pw-link` (without them pyusic works, just without it).

```sh
git clone <this repo> && cd pyusic
./install.sh        # venv + ~/.local/bin/pyusic
pyusic
```

## Keys

| key | action |
|---|---|
| `space` | play / pause |
| `n` `p` | next / previous |
| `←` `→` | seek 5 s (shift: 30 s) |
| `+` `-` `m` | volume / mute |
| `s` `r` | shuffle / repeat (off → all → one) |
| `v` `c` | visualizer: next style / next colour scheme |
| `w` | visualizer: full width ↔ inside the player panel |
| `b` | beat effect on/off |
| `[` `]` | visualizer sensitivity |
| `a` | add a playlist URL or a local folder |
| `g` | search YouTube |
| `/` | filter the track list |
| `D` | download the list (folder or zip), `x` cancels |
| `u` `d` | refresh / remove the highlighted playlist |
| `?` | help |

`pyusic <playlist-url>` adds and opens a playlist straight away.

## Visualizer

Styles (`v`): `bars` (with falling peak caps), `thin` (braille, one dot per bar), `mirror`, `wave` (oscilloscope), `vu` (stereo meters), `off`.
Colours (`c`): `theme` (follows your Omarchy accent), `rainbow`, `fire`, `ice`, `mono`. `[` / `]` change the sensitivity.
`w` moves the visualizer between the player panel and a full-width band under the whole window.
Your choices are saved.

Beat effect (`b`): a kick detector watches the bass (40-170 Hz). On each kick the bars flash towards white, the bass end
punches up, and the visualizer's frame lights up. The cover is deliberately left alone: redrawing a sixel image on every
beat would be expensive.

How it works: pyusic opens a PipeWire capture stream and links only mpv's output to it, so it sees pyusic's audio and nothing else
(the sound still goes to your speakers). Each frame it runs an FFT on the last 2048 samples, groups the result into log-spaced
frequency bands (bass on the left, treble on the right) and draws one bar per band. An automatic gain keeps it readable at any volume.
The capture stream is only open while the visualizer is on and music is playing, so it costs nothing otherwise.

## Data

- Library: `~/.config/pyusic/library.json`
- Cache: `~/.cache/pyusic/`
- Downloads: `~/Music/pyusic/<playlist>/` (`$XDG_MUSIC_DIR` is respected)

## Remote control

While running, pyusic serves a control socket and a status file under `$XDG_RUNTIME_DIR/pyusic/`.

```sh
pyusic ctl toggle | next | prev | prevtrack | seek 10 | seekto 90 | volume -5 | volumeto 40 | shuffle | repeat | mute
```

Handy for keybindings and bar widgets. `status.json` holds title, artist, position, volume, etc.

## Omarchy bar widget

```sh
./omarchy-widget/install.sh
```

Fixed-width now-playing item next to the clock: cover, title, progress line.
Left click opens a player popup, middle click toggles play/pause, right click opens pyusic, the wheel changes track.
Remove with `omarchy plugin disable pyus.pyusic`.
