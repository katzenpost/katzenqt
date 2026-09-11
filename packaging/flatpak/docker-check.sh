#!/bin/sh
set -eu

addr=${1:-127.0.0.1:64331}
python3 - "$addr" <<'PYTHON' || {
import socket
import sys

host, port = sys.argv[1].rsplit(":", 1)
with socket.create_connection((host, int(port)), 1):
    pass
PYTHON
    printf '%s\n' "Docker kpclientd is unavailable at $addr." \
        "Start it with: cd katzenpost/docker && make base_port=62331 start wait" >&2
    exit 1
}
