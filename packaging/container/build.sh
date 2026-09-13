#!/bin/sh
set -eu

here=$(CDPATH= cd -- "$(dirname "$0")" && pwd)
root=$(CDPATH= cd -- "$here/../.." && pwd)
cd "$root"
distro="${1:-ubuntu-26.04}"
override="packaging/container/overrides/$distro.env"
test -f "$override" || { printf '%s\n' "unknown distro: $distro" >&2; exit 1; }
. "./$override"

if [ "${SKIP:-}" != "" ]; then
	printf '%s\n' "skipping $distro: $SKIP" >&2
	exit 0
fi

PODMAN="${PODMAN:-podman}"
image="katzenqt-deb-$distro"
out="dist/$distro"

"$PODMAN" build --build-arg BASE="$BASE" --build-arg BUILD_DEPS="$BUILD_DEPS" \
	-t "$image" -f packaging/container/Containerfile packaging/container
mkdir -p "$out"
"$PODMAN" run --rm -v "$PWD:/src:ro,z" -v "$PWD/$out:/out:rw,z" "$image" sh -c '
	cp -a /src /build/katzenqt
	cd /build/katzenqt
	rm -rf dist
	packaging/debian/build.sh
	cp dist/katzenqt_*.deb /out/
'
sha256sum "$out"/katzenqt_*.deb
