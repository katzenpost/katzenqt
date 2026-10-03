#!/bin/sh
set -eu

debs=${DEBS_DIR:-/tmp/debs}
mkdir -p "$debs"

apt update
apt install -y --no-install-recommends \
    build-essential debhelper dh-python python3-all \
    pybuild-plugin-pyproject flit \
    dpkg-dev golang-go git ca-certificates systemd pkg-config cmake \
    python3-dev python3-venv python3-pip cargo rustc unzip \
    libasound2-dev

packaging/container/pydeps-build.sh "$debs"
packaging/container/kpclientd-build.sh "$debs"
packaging/debian/build.sh
cp dist/katzenqt_*.deb "$debs"/

apt install -y \
    "$debs"/python3-pprintpp_*.deb \
    "$debs"/python3-pycrdt_*.deb \
    "$debs"/python3-rustic-audio-tool_*.deb \
    "$debs"/python3-katzenpost-thinclient_*.deb \
    "$debs"/katzenpost_*.deb \
    "$debs"/katzenqt_0.0.1_all.deb

packaging/debian/smoke.sh
packaging/debian/assert-reproducible.sh "$debs"
