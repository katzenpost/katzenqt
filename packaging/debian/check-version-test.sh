#!/bin/sh
set -eu

root=$(mktemp -d "${TMPDIR:-/var/tmp}/kqt-check-version.XXXXXX")
trap 'rm -rf "$root"' EXIT
script=$(cd "$(dirname "$0")" && pwd)/check-version.sh
failed=0
n=0

tree() {
    n=$((n + 1))
    dir=$root/$n
    mkdir -p "$dir/packaging/debian" "$dir/packaging/flatpak" "$dir/debian"
    cp "$script" "$dir/packaging/debian/check-version.sh"
    printf 'version = "%s"\n' "$1" > "$dir/pyproject.toml"
    printf 'katzenqt (%s) unstable; urgency=medium\n' "$2" \
        > "$dir/debian/changelog"
    printf '  <release version="%s" date="2026-10-15"/>\n' "$3" \
        > "$dir/packaging/flatpak/network.katzenpost.katzenqt.metainfo.xml"
}

expect() {
    want=$1
    name=$2
    shift 2
    if "$dir/packaging/debian/check-version.sh" "$@" >/dev/null 2>&1; then
        got=0
    else
        got=1
    fi
    if [ "$got" = "$want" ]; then
        printf 'ok   %s\n' "$name"
    else
        printf 'FAIL %s: wanted %s, got %s\n' "$name" "$want" "$got"
        failed=1
    fi
}

tree 0.0.3 0.0.3 0.0.3
expect 0 "three agreeing versions pass"
expect 0 "and pass against their own tag" 0.0.3
expect 1 "but fail against another tag" 0.0.4

tree 0.0.3 0.0.2 0.0.3
expect 1 "a stale changelog fails"

tree 0.0.3 0.0.3 0.0.1
expect 1 "a stale metainfo fails"

tree 0.0.3 0.0.3 0.0.3
rm "$dir/debian/changelog"
expect 1 "a missing changelog fails"

exit "$failed"
