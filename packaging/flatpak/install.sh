#!/bin/sh
set -eu
root=$(CDPATH= cd -- "$(dirname "$0")/../.." && pwd)
flatpak install --user --reinstall -y "$root/.flatpak-repo" network.katzenpost.katzenqt
