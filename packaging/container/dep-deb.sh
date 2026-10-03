#!/bin/sh
set -eu

out=${1:?usage: dep-deb.sh OUTDIR URL REV}
url=${2:?usage: dep-deb.sh OUTDIR URL REV}
rev=${3:?usage: dep-deb.sh OUTDIR URL REV}
root=$(CDPATH= cd -- "$(dirname "$0")/../.." && pwd)

mkdir -p "$out"
work=$(mktemp -d)
git clone --quiet "$url" "$work/src"
git -C "$work/src" -c advice.detachedHead=false switch --detach "$rev"
SOURCE_DATE_EPOCH=$(dpkg-parsechangelog \
    -l "$root/debian/changelog" -STimestamp)
export SOURCE_DATE_EPOCH
$(command -v make) -C "$work/src" deb-build
cp "$work/src"/dist/*.deb "$out"/
rm -rf "$work"
