#!/bin/sh
set -eu

here=$(CDPATH= cd -- "$(dirname "$0")" && pwd)
root=$(CDPATH= cd -- "$here/../.." && pwd)
id=network.katzenpost.katzenqt
epoch=1789130760
timestamp=2026-09-11T12:46:00Z
screenshot="$here/screenshots/katzenqt.png"
media_url=https://dl.flathub.org/media/network/katzenpost/katzenqt/katzenqt.png

build_all() {
	cd "$root"
	rm -rf .flatpak-build .flatpak-export .flatpak-repo
	flatpak-builder --force-clean --override-source-date-epoch="$epoch" \
		--repo=.flatpak-export .flatpak-build "packaging/flatpak/$id.yaml"
	python3 "$here/mirror-screenshot.py" catalog .flatpak-build "$screenshot" "$media_url" "$timestamp"
	flatpak build-export --update-appstream --timestamp="$timestamp" \
		.flatpak-repo .flatpak-build master
	python3 "$here/mirror-screenshot.py" repo .flatpak-repo "$screenshot" "$media_url" "$timestamp"
	flatpak build-update-repo --no-update-appstream .flatpak-repo
}

if [ "${KQT_REBUILD:-}" = "1" ]; then
	build_all
	exit 0
fi

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

command -v podman >/dev/null 2>&1 || {
	printf '%s\n' "podman is required to build the flatpak" \
		"run: make flatpak-system-deps" >&2
	exit 1
}
git -C "$root" archive --format=tar.gz -o "$here/katzenqt-src.tar.gz" HEAD
exec "$here/container/build.sh"
