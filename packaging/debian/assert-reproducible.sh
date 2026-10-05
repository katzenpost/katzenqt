#!/bin/sh
set -eu

debs=${1:-/tmp/debs}
a=$(sha256sum "$debs"/katzenqt_*.deb | cut -d' ' -f1)

rm -rf /tmp/rb
cp -a . /tmp/rb
( cd /tmp/rb && rm -rf dist && packaging/debian/build.sh >/dev/null )
b=$(sha256sum /tmp/rb/dist/katzenqt_*.deb | cut -d' ' -f1)

printf 'first  %s\nsecond %s\n' "$a" "$b"
test "$a" = "$b"
