#!/bin/sh
set -eu

here=$(CDPATH= cd -- "$(dirname "$0")" && pwd)
root=$(CDPATH= cd -- "$here/../.." && pwd)
id=network.katzenpost.katzenqt
epoch=1787647836
timestamp=2026-08-25T08:50:36Z

build_all() {
	cd "$root"
	rm -rf .flatpak-build .flatpak-export .flatpak-repo "$here/katzenqt-src.tar.gz"
	git archive --format=tar.gz -o "$here/katzenqt-src.tar.gz" HEAD
	flatpak-builder --force-clean --override-source-date-epoch="$epoch" \
		--repo=.flatpak-export .flatpak-build "packaging/flatpak/$id.yaml"
	flatpak build-export --update-appstream --timestamp="$timestamp" \
		.flatpak-repo .flatpak-build master
	flatpak build-update-repo --no-update-appstream .flatpak-repo
}

# second build for the reproducibility check
if [ "${KQT_REBUILD:-}" = "1" ]; then
	build_all
	exit 0
fi

# inside the container: run the real build and the folded checks. the image
# already carries the toolchain and the runtime, so there is nothing to probe.
if [ "${KQT_FLATPAK_INNER:-}" = "1" ]; then
	build_all
	"$here/lint.sh" "packaging/flatpak/$id.yaml" .flatpak-repo
	KQT_REBUILD=1 "$here/reproducible.sh" .flatpak-repo "$here/build.sh"
	appstreamcli validate --no-net "packaging/flatpak/$id.metainfo.xml"
	desktop-file-validate "packaging/$id.desktop"
	flatpak-builder --run .flatpak-build "packaging/flatpak/$id.yaml" \
		python3 - imports < "$here/check.py"
	exit 0
fi

# on the host: the build and every check run inside podman so the heavy flatpak
# toolchain never touches the host. only podman is required here; the driver
# builds the image and runs this script again with KQT_FLATPAK_INNER=1.
command -v podman >/dev/null 2>&1 || {
	printf '%s\n' "podman is required to build the flatpak" \
		"run: make flatpak-system-deps" >&2
	exit 1
}
exec "$here/container/build.sh"
