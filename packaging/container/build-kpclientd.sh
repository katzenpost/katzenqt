#!/bin/sh
set -eu

here=$(CDPATH= cd -- "$(dirname "$0")" && pwd)
root=$(CDPATH= cd -- "$here/../.." && pwd)
cd "$root"
distro="${1:-ubuntu-26.04}"
override="packaging/container/overrides/$distro.env"
test -f "$override" || { printf '%s\n' "unknown distro: $distro" >&2; exit 1; }
. "./$override"

PODMAN="${PODMAN:-podman}"
image="katzenqt-kpclientd-$distro"
out="dist/$distro"

"$PODMAN" build --build-arg BASE="$BASE" -t "$image" \
	-f packaging/container/Containerfile.kpclientd packaging/container
mkdir -p "$out"
"$PODMAN" run --rm -v "$PWD:/src:ro,z" -v "$PWD/$out:/out:rw,z" "$image" \
	/src/packaging/container/kpclientd-build.sh /out
sha256sum "$out"/kpclientd_*.deb
