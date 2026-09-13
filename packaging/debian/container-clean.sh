#!/bin/sh
set -eu

podman="${PODMAN:-podman}"
ids=$("$podman" images -q 'katzenqt-deb-*' 2>/dev/null || true)
if [ -n "$ids" ]; then "$podman" rmi -f $ids; fi
