#!/bin/sh
set -eu

out=${1:?usage: dep-deb.sh OUTDIR URL REV [TARGET] [DEBS]}
url=${2:?usage: dep-deb.sh OUTDIR URL REV [TARGET] [DEBS]}
rev=${3:?usage: dep-deb.sh OUTDIR URL REV [TARGET] [DEBS]}
target=${4:-deb-build}
debs=${5:-*.deb}
root=$(CDPATH= cd -- "$(dirname "$0")/../.." && pwd)
. "$(dirname "$0")/retry.sh"

mkdir -p "$out"
work=$(mktemp -d)
clone_once() {
    rm -rf "$work/src"
    git clone --quiet "$url" "$work/src"
}
retry clone_once
if ! git -C "$work/src" cat-file -e "$rev^{commit}" 2>/dev/null; then
    printf '%s\n' "$url has no $rev" \
        "a force-push may have discarded it; repoint the pin" >&2
    exit 1
fi
git -C "$work/src" -c advice.detachedHead=false switch --detach "$rev"
SOURCE_DATE_EPOCH=$(dpkg-parsechangelog \
    -l "$root/debian/changelog" -STimestamp)
export SOURCE_DATE_EPOCH
GIT_CONFIG_COUNT=1
GIT_CONFIG_KEY_0=safe.directory
GIT_CONFIG_VALUE_0="$work/src"
export GIT_CONFIG_COUNT GIT_CONFIG_KEY_0 GIT_CONFIG_VALUE_0
build_once() {
    git -C "$work/src" clean -xdq
    "$(command -v make)" -C "$work/src" "$target"
}
retry build_once
cp "$work/src"/dist/$debs "$out"/
rm -rf "$work"
