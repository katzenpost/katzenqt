#!/bin/sh
set -eu

out=${1:?usage: dep-deb.sh OUTDIR URL REV [TARGET]}
url=${2:?usage: dep-deb.sh OUTDIR URL REV [TARGET]}
rev=${3:?usage: dep-deb.sh OUTDIR URL REV [TARGET]}
target=${4:-deb-build}
root=$(CDPATH= cd -- "$(dirname "$0")/../.." && pwd)

mkdir -p "$out"
work=$(mktemp -d)
cloned=
for _ in 1 2 3; do
    rm -rf "$work/src"
    if git clone --quiet "$url" "$work/src"; then
        cloned=yes
        break
    fi
    sleep 5
done
if [ -z "$cloned" ]; then
    printf '%s\n' "could not clone $url" >&2
    exit 1
fi
if ! git -C "$work/src" cat-file -e "$rev^{commit}" 2>/dev/null; then
    printf '%s\n' "$url has no $rev" \
        "a force-push may have discarded it; repoint the pin" >&2
    exit 1
fi
git -C "$work/src" -c advice.detachedHead=false switch --detach "$rev"
SOURCE_DATE_EPOCH=$(dpkg-parsechangelog \
    -l "$root/debian/changelog" -STimestamp)
export SOURCE_DATE_EPOCH
git config --global --add safe.directory "$work/src"
$(command -v make) -C "$work/src" "$target"
cp "$work/src"/dist/*.deb "$out"/
rm -rf "$work"
