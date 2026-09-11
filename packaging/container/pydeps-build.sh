#!/bin/sh
set -eu

out=${1:?usage: pydeps-build.sh OUTDIR}
root=$(CDPATH= cd -- "$(dirname "$0")/../.." && pwd)
mk="$root/Makefile"
PYCRDT_URL=$(sed -n 's/^PYCRDT_URL := //p' "$mk")
PYCRDT_REV=$(sed -n 's/^PYCRDT_REV := //p' "$mk")
THIN_VER=$(sed -n 's/^THINCLIENT_VER := //p' "$mk")
PPRINTPP_VER=$(sed -n 's/^PPRINTPP_VER := //p' "$mk")
PIP_VER=$(sed -n 's/^PIP_VER := //p' "$mk")
MATURIN_VER=$(sed -n 's/^MATURIN_VER := //p' "$mk")

SOURCE_DATE_EPOCH=$(dpkg-parsechangelog -l "$root/debian/changelog" -STimestamp)
export SOURCE_DATE_EPOCH
RUSTIC_AUDIO_URL=$(sed -n 's/^RUSTIC_AUDIO_URL := //p' "$mk")
RUSTIC_AUDIO_REV=$(sed -n 's/^RUSTIC_AUDIO_REV := //p' "$mk")

mkdir -p "$out"
work=$(mktemp -d)
python3 -m venv "$work/venv"
PIP="$work/venv/bin/pip"
"$PIP" install -q "pip==$PIP_VER"
constraints="$work/constraints.txt"
printf 'maturin==%s\n' "$MATURIN_VER" > "$constraints"
export PIP_CONSTRAINT="$constraints"

build_deb() {
	debname="$1"; arch="$2"; wheeldir="$3"; depends="$4"
	pkg="$work/pkg-$debname"
	mkdir -p "$pkg/DEBIAN" "$pkg/usr/lib/python3/dist-packages"
	( cd "$pkg/usr/lib/python3/dist-packages" && for w in "$wheeldir"/*.whl; do unzip -oq "$w"; done )
	ver=$(ls "$wheeldir"/*.whl | head -1 | sed -E "s#.*/[^-]+-([^-]+)-.*#\1#")
	cat > "$pkg/DEBIAN/control" <<CTRL
Package: $debname
Version: $ver
Architecture: $arch
Maintainer: Katzenpost <katzenqt@katzenpost.network>
Depends: $depends
Section: python
Priority: optional
Description: $debname built from pinned source for katzenqt
 Vendored dependency, not yet in Debian.
CTRL
	dpkg-deb --build --root-owner-group "$pkg" "$out/${debname}_${ver}_${arch}.deb"
}

git clone --quiet "$PYCRDT_URL" "$work/pycrdt"
git -C "$work/pycrdt" -c advice.detachedHead=false switch --detach "$PYCRDT_REV"
mkdir -p "$work/wh-pycrdt"
"$PIP" wheel --no-deps -w "$work/wh-pycrdt" "$work/pycrdt"
build_deb python3-pycrdt amd64 "$work/wh-pycrdt" "python3, python3-anyio"

git clone --quiet "$RUSTIC_AUDIO_URL" "$work/audio"
git -C "$work/audio" -c advice.detachedHead=false switch --detach "$RUSTIC_AUDIO_REV"
mkdir -p "$work/wh-audio"
"$PIP" wheel --no-deps -w "$work/wh-audio" "$work/audio"
build_deb python3-rustic-audio-tool amd64 "$work/wh-audio" \
	"python3, libasound2t64 | libasound2"

mkdir -p "$work/wh-pprintpp"
"$PIP" wheel --no-deps --only-binary :all: -w "$work/wh-pprintpp" "pprintpp==$PPRINTPP_VER"
if unzip -l "$work"/wh-pprintpp/*.whl | grep -qE "\.(so|pyd)$"; then
	printf '%s\n' "pprintpp wheel ships a compiled object; refusing" >&2
	exit 1
fi
build_deb python3-pprintpp all "$work/wh-pprintpp" "python3"

mkdir -p "$work/wh-thin"
"$PIP" wheel --no-deps --no-binary :all: -w "$work/wh-thin" "katzenpost_thinclient==$THIN_VER"
build_deb python3-katzenpost-thinclient all "$work/wh-thin" \
	"python3, python3-cbor2, python3-coloredlogs, python3-pprintpp, python3-toml"
