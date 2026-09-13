#!/bin/sh
set -eu

manifest=$1
repo=$2

if command -v flatpak-builder-lint >/dev/null 2>&1; then
	flatpak-builder-lint manifest "$manifest"
	flatpak-builder-lint repo "$repo"
elif flatpak info org.flatpak.Builder >/dev/null 2>&1; then
	flatpak run --filesystem="$PWD" --command=flatpak-builder-lint org.flatpak.Builder manifest "$manifest"
	flatpak run --filesystem="$PWD" --command=flatpak-builder-lint org.flatpak.Builder repo "$repo"
else
	printf '%s\n' 'flatpak-builder-lint is unavailable; install it or org.flatpak.Builder'
	exit 1
fi
