#!/bin/sh
set -eu

root=$(cd "$(dirname "$0")/../.." && pwd)
tag=${1-}
metainfo=$root/packaging/flatpak/network.katzenpost.katzenqt.metainfo.xml

die() {
    printf '%s\n' "$*" >&2
    exit 1
}

pyproject=$(sed -n 's/^version = "\([0-9][0-9.]*\)"$/\1/p' \
    "$root/pyproject.toml" | head -n 1)
test -n "$pyproject" || die "pyproject.toml: no version"

changelog=$(sed -n '1s/^[^ ]* (\([^)]*\)).*/\1/p' "$root/debian/changelog")
test -n "$changelog" || die "debian/changelog: no version on line 1"

appstream=$(sed -n 's/.*<release version="\([0-9][0-9.]*\)".*/\1/p' \
    "$metainfo" | head -n 1)
test -n "$appstream" || die "metainfo: no release version"

test "$changelog" = "$pyproject" \
    || die "debian/changelog $changelog != pyproject $pyproject"
test "$appstream" = "$pyproject" \
    || die "metainfo $appstream != pyproject $pyproject"

test -z "$tag" || test "$pyproject" = "$tag" \
    || die "version $pyproject != tag $tag"
