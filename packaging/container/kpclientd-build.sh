#!/bin/sh
set -eu

out=${1:?usage: kpclientd-build.sh OUTDIR}
root=$(CDPATH= cd -- "$(dirname "$0")/../.." && pwd)
mk="$root/Makefile"
REV=$(sed -n 's/^KATZENPOST_REV := //p' "$mk")
URL=$(sed -n 's/^KATZENPOST_URL := //p' "$mk")
arch=$(dpkg --print-architecture)

mkdir -p "$out"
work=$(mktemp -d)
git clone --quiet "$URL" "$work/kp"
git -C "$work/kp" -c advice.detachedHead=false switch --detach "$REV"
SOURCE_DATE_EPOCH=$(dpkg-parsechangelog -l "$root/debian/changelog" -STimestamp)
export SOURCE_DATE_EPOCH
( cd "$work/kp/cmd/kpclientd" && CGO_ENABLED=1 GOFLAGS=-trimpath go build -o "$work/kpclientd" )
pkg="$work/pkg"
mkdir -p "$pkg/DEBIAN" "$pkg/usr/bin" "$pkg/etc/kpclientd"
install -m0755 "$work/kpclientd" "$pkg/usr/bin/kpclientd"
install -m0644 "$root/src/katzenqt/data/client.toml" "$pkg/etc/kpclientd/client.toml"
sed "s/^Architecture: .*/Architecture: $arch/" \
	"$root/packaging/debian/kpclientd/control" > "$pkg/DEBIAN/control"
dpkg-deb --build --root-owner-group "$pkg" "$out/kpclientd_0.0.1_${arch}.deb"
