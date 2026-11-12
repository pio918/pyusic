#!/bin/sh
# Installs the pyusic bar widget into the Omarchy shell (user plugin, no system files touched).
set -e
here="$(cd "$(dirname "$(readlink -f "$0")")" && pwd)"
dest="$HOME/.config/omarchy/plugins/pyus.pyusic"
mkdir -p "$dest"
cp "$here/pyus.pyusic/manifest.json" "$here/pyus.pyusic/BarWidget.qml" "$dest/"
omarchy plugin validate "$dest"
omarchy plugin enable pyus.pyusic --section centerBegin
echo "widget enabled. Remove with: omarchy plugin disable pyus.pyusic"
