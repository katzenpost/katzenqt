#!/bin/sh
set -eu
: "${TAG:?set TAG=vX.Y.Z, e.g. make flatpak-release TAG=v0.0.1}"
here=$(CDPATH= cd -- "$(dirname "$0")" && pwd)
dest=${FLATHUB_DIST:-.flathub-dist}

python3 "$here/release.py" validate --tag "$TAG" --remote
python3 "$here/release.py" dist --tag "$TAG" --destination "$dest" --remote
python3 "$here/release.py" check --tag "$TAG" --destination "$dest"
python3 "$here/release.py" submit --tag "$TAG" --destination "$dest"
