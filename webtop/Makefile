.PHONY: start stop clean launch-3

# start / stop / clean run on the HOST (podman/podman-compose).
# launch-3 runs INSIDE the webtop container, from a desktop terminal:
#   cd /config/katzenqt/webtop && make launch-3
# The repo is mounted read-write into the container at /config/katzenqt.
start:
	HOST_GID=$(shell id -g) HOST_UID=$(shell id -u) podman-compose up -d --build
stop:
	podman-compose down
clean:
	rm -rf ./uv_cache

# Launch three fresh katzenqt clients (KQT_STATE a/b/c), each dialing
# kpclientd through host_localhost:64331 (see thinclient-webtop.toml).
# Logs land in the repo mount as /config/katzenqt/{a,b,c}.log.
#
# UV_PROJECT_ENVIRONMENT keeps the container's venv OUT of the repo's
# .venv (which belongs to host-side runs); the container venv lives at
# /config/.venv-katzenqt and persists across container restarts.
launch-3:
	cd /config/katzenqt && for x in a b c; do \
		KQT_STATE=$$x \
		KATZENQT_THINCLIENT_CONFIG=/config/katzenqt/webtop/thinclient-webtop.toml \
		UV_PROJECT_ENVIRONMENT=/config/.venv-katzenqt \
		uv run katzenqt >> $$x.log 2>&1 & \
		sleep 2; \
	done
