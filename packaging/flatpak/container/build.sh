#!/bin/sh
set -eu

here=$(CDPATH= cd -- "$(dirname "$0")" && pwd)
root=$(CDPATH= cd -- "$here/../../.." && pwd)
cd "$root"

PODMAN="${PODMAN:-podman}"
image=katzenqt-flatpak

"$PODMAN" build -t "$image" \
	-f packaging/flatpak/container/Containerfile packaging/flatpak/container

"$PODMAN" run --rm --privileged --device /dev/fuse \
	-v "$root:/src:z" -w /src \
	-e KQT_FLATPAK_INNER=1 \
	"$image" packaging/flatpak/build.sh
