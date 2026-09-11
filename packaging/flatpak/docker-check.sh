#!/bin/sh
set -eu

addr=${1:-127.0.0.1:64331}
host=${addr%%:*}
port=${addr##*:}
python3 -c "import socket; socket.create_connection(('$host', $port), 1).close()" 2>/dev/null || {
	printf '%s\n' "Docker kpclientd is unavailable at $addr." \
		"Start it with: cd katzenpost/docker && make start wait" >&2
	exit 1
}
