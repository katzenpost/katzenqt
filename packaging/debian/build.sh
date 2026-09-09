#!/bin/sh
set -eu

cd "$(dirname "$0")/../.."
dpkg-buildpackage -b -uc -us "$@"
mkdir -p dist
mv ../katzenqt_*.deb dist/
sha256sum dist/katzenqt_*.deb
