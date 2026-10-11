#!/bin/sh
set -eu

repo=$1
shift

refs() {
	ostree refs --repo="$repo" | sort | while read -r ref; do
		printf '%s %s\n' "$ref" "$(ostree --repo="$repo" rev-parse "$ref")"
	done
}

first=$(refs)
"$@" >/dev/null
second=$(refs)
test "$first" = "$second"
printf '%s\n' "$second"
