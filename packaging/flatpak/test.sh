#!/bin/sh
set -eu

here=$(CDPATH= cd -- "$(dirname "$0")" && pwd)
root=$(CDPATH= cd -- "$here/../.." && pwd)
cd "$root"

id=network.katzenpost.katzenqt
: "${MAKE:=make}"
: "${KATZENPOST_DIR:=katzenpost}"
: "${UV:=uv}"
: "${FLATPAK_DOCKER_ADDRESS:=127.0.0.1:64331}"
docker="$KATZENPOST_DIR/docker"
runner="$here/integration-python"

# sandbox checks against the installed app
flatpak run --command=python3 "$id" - imports < "$here/check.py"
flatpak run --command=python3 "$id" - permissions < "$here/check.py"
flatpak run --command=sh "$id" -c 'test ! -e /app/bin/kpclientd'
flatpak run --command=python3 --env=KQT_STATE=flatpak-test "$id" - state flatpak-test < "$here/check.py"

started=
cleanup() {
	if [ -n "$started" ]; then "$MAKE" -C "$docker" stop; fi
}
trap cleanup EXIT INT TERM

if "$here/docker-check.sh" "$FLATPAK_DOCKER_ADDRESS" >/dev/null 2>&1; then
	test -f "$docker/voting_mixnet/running.stamp"
	"$MAKE" -C "$docker" ps | grep -Eq '(^|[[:space:]])kpclientd([[:space:]]|$)'
else
	started=1
	"$MAKE" -C "$docker" start wait
fi

"$MAKE" setup-uv
"$here/docker-check.sh" "$FLATPAK_DOCKER_ADDRESS"
KATZENQT_INTEGRATION_PYTHON="$runner" "$here/wait-for-mixnet"
KATZENQT_DOCKER_INTEGRATION=1 KATZENQT_INTEGRATION_PYTHON="$runner" "$UV" run pytest --no-cov tests/integration

if [ -n "$started" ]; then
	cleanup
	started=
	if "$here/docker-check.sh" "$FLATPAK_DOCKER_ADDRESS" >/dev/null 2>&1; then
		printf '%s\n' 'error: managed test mixnet is still reachable' >&2
		exit 1
	fi
fi
