#!/bin/sh
set -eu

out=${1:?usage: kpclientd-build.sh OUTDIR}
root=$(CDPATH= cd -- "$(dirname "$0")/../.." && pwd)
mk="$root/Makefile"
REV=$(sed -n 's/^KATZENPOST_REV := //p' "$mk")
URL=$(sed -n 's/^KATZENPOST_URL := //p' "$mk")

mkdir -p "$out"
work=$(mktemp -d)
git clone --quiet "$URL" "$work/kp"
git -C "$work/kp" -c advice.detachedHead=false switch --detach "$REV"

DEBS_DIR="$out" "$work/kp/packaging/debian/ci.sh"
