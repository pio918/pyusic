#!/bin/sh
# Installs the ytplay bar widget into the Omarchy shell (user plugin, no system files touched).
set -e
here="$(cd "$(dirname "$(readlink -f "$0")")" && pwd)"
dest="$HOME/.config/omarchy/plugins/pyus.ytplay"
mkdir -p "$dest"
cp "$here/pyus.ytplay/manifest.json" "$here/pyus.ytplay/BarWidget.qml" "$dest/"
omarchy plugin validate "$dest"
omarchy plugin enable pyus.ytplay --section centerBegin
echo "widget enabled. Remove with: omarchy plugin disable pyus.ytplay"
