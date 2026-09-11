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

"$PODMAN" build --build-arg BASE="$BASE" --build-arg BUILD_DEPS="$BUILD_DEPS" \
	-t "$image" -f packaging/container/Containerfile packaging/container
"$PODMAN" run --rm -v "$PWD:/src:ro,z" "$image" sh -eu -c '
	cp -a /src /build/a
	cp -a /src /build/b
	rm -rf /build/a/dist /build/b/dist
	( cd /build/a && packaging/debian/build.sh >/dev/null )
	( cd /build/b && packaging/debian/build.sh >/dev/null )
	first=$(sha256sum /build/a/dist/katzenqt_*.deb | cut -d" " -f1)
	second=$(sha256sum /build/b/dist/katzenqt_*.deb | cut -d" " -f1)
	test "$first" = "$second"
	printf "%s\n" "$first"
'
