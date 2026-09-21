SHELL := /bin/bash
.ONESHELL:
.SHELLFLAGS := -eu -o pipefail -c

.DEFAULT_GOAL := help
MAKEFLAGS += --no-print-directory

export PATH:=$(PATH):~/.local/bin/
export UV_VENV_CLEAR:=1
# override uv with:
#   make setup-uv UV=$$HOME/.local/bin/uv
UV ?= uv

VENV := .venv
BACKEND_UV := $(VENV)/.backend-uv
BACKEND_PIP := $(VENV)/.backend-pip
STAMP_UV := $(VENV)/.setup-uv.stamp
STAMP_PIP := $(VENV)/.setup-pip.stamp

SYSTEM_STAMP := .system-setup.stamp

KATZENPOST_DIR := katzenpost
KATZENPOST_URL := https://github.com/katzenpost/katzenpost.git

GEN_RES := src/katzenqt/resources_rc.py
GEN_UI_MIX := src/katzenqt/ui_mixchat.py
GEN_UI_FONT := src/katzenqt/ui_font_settings.py

PYPROJECT := pyproject.toml
UV_LOCK := $(wildcard uv.lock)

# make alembic-revision-uv ALEMBIC_MSG='some changeset details'
ALEMBIC_MSG ?=
ALEMBIC_MSG_Q := "$(ALEMBIC_MSG)"

# make docker-integration KQT_INTEGRATION_PARALLEL=2
# Number of pytest-xdist workers for the non-serial_docker phase of
# docker-integration. Default 4 matches CI's worker count (see the
# workflow); CI's ~2.5-3x slower mixnet is absorbed by the raised 240s
# _send_one_gcm floor rather than by dropping concurrency.
KQT_INTEGRATION_PARALLEL ?= 4

.PHONY: default default_uv_setup default_pip_setup help \
	system-setup install-debian-packages install-uv clean-system-stamp \
	setup setup-uv setup-pip setup-status \
	run test status code-generator regen-code \
	run-uv run-pip test-uv test-pip \
	alembic-check-uv alembic-check-pip \
	alembic-revision-uv alembic-revision-pip \
	katzenpost-update kpclientd kpclientd-podman install-kpclient kpclientd.service \
	clean clean-venv deps deps-audio

deps: deps-audio default_uv_setup

default: default_uv_setup

default_uv_setup: system-setup setup-uv setup test kpclientd install-kpclient \
		kpclientd.service status
default_pip_setup: system-setup setup-pip setup test kpclientd install-kpclient \
		kpclientd.service status

help:
	@printf '%s\n' \
		'If in doubt, run `make deps` and `source ~/.bashrc || source ~/.profile` and `make run`' \
		'' \
		'Usage:' \
		'  make deps                  Install system packages and venv' \
		'  make system-setup          Install system packages (Debian/Ubuntu) and uv (via pipx)' \
		'  make setup-uv              Create or update .venv using uv' \
		'  make setup-pip             Create or update .venv using pip/venv' \
		'' \
		'Backend auto selection:' \
		'  make setup                 Ensure setup is complete for the chosen backend and print status' \
		'  make run                   Run katzenqt using the chosen backend' \
		'  make test                  Run pytest using the chosen backend' \
		'  make status                Show backend, venv, and kpclientd status' \
		'' \
		'Code generation:' \
		'  make code-generator        Generate Qt code only if needed (missing or inputs changed)' \
		'  make regen-code            Force regenerate Qt code' \
		'  make alembic-check-uv      Alembic check using uv'\
		'  make alembic-check-pip     Alembic check using pip'\
		'  make alembic-revision-uv   Alembic revision using uv (requires ALEMBIC_MSG="msg")'\
		'  make alembic-revision-pip  Alembic revision using pip (requires ALEMBIC_MSG="msg")'\
		'' \
		'Katzenpost / kpclientd:' \
		'  make katzenpost-update     git pull --ff-only in ./katzenpost (clone if missing)' \
		'  make kpclientd             Build kpclientd (golang native build; falls back to podman)' \
		'  make kpclientd-podman      Build kpclientd using the container toolchain' \
		'  make install-kpclient      Install kpclientd to ~/.local/bin/kpclientd' \
		'  make kpclientd.service     Install and enable user systemd service for kpclientd' \
		'' \
		'Maintenance:' \
		'  make clean-venv            Remove only .venv and force setup next time' \
		'  make clean                 Remove .venv, stamps, and generated Qt files'

system-setup: $(SYSTEM_STAMP)

$(SYSTEM_STAMP):
	@$(MAKE) install-debian-packages
	@$(MAKE) install-uv
	@touch $(SYSTEM_STAMP)

clean-system-stamp:
	@rm -f $(SYSTEM_STAMP)

install-debian-packages:
	@sudo apt install -y \
		libxcb-cursor0 libegl1 libpulse0 libfontconfig1 libxkbcommon0 \
		build-essential pkg-config \
		git podman \
		pipx python3 python3-venv >/dev/null

install-uv:
	@pipx install -f uv >/dev/null

deps-audio:
	@sudo apt install -y libasound2-dev cargo >/dev/null

setup:
	@$(MAKE) setup-status

setup-status:
	@if [[ -e "$(BACKEND_UV)" ]]; then \
		$(MAKE) setup-uv >/dev/null; \
		printf '%s\n' "setup: ok (backend=uv, venv=$(VENV))"; \
	elif [[ -e "$(BACKEND_PIP)" ]]; then \
		$(MAKE) setup-pip >/dev/null; \
		printf '%s\n' "setup: ok (backend=pip, venv=$(VENV))"; \
	else \
		printf '%s\n' "setup: not configured (run: make setup-uv OR make setup-pip)"; \
		exit 1; \
	fi

setup-uv: $(STAMP_UV)

setup-pip: $(STAMP_PIP)

$(STAMP_UV): system-setup $(PYPROJECT) $(UV_LOCK)
	@if [[ -e "$(BACKEND_PIP)" ]]; then \
		printf '%s\n' "error: .venv is pip-managed; run 'make clean-venv' first"; \
		exit 1; \
	fi
	@command -v "$(UV)" >/dev/null 2>&1 || { printf '%s\n' "error: uv not found (set UV=/path/to/uv)"; exit 1; }
	@if [[ ! -f "$(VENV)/pyvenv.cfg" ]]; then \
		$(UV) venv "$(VENV)"; \
	fi
	@$(UV) pip install . >/dev/null 2>&1
	@$(UV) pip install -U pytest >/dev/null 2>&1
	@touch $(BACKEND_UV)
	@touch $(STAMP_UV)

$(STAMP_PIP): system-setup $(PYPROJECT)
	@if [[ -e "$(BACKEND_UV)" ]]; then \
		printf '%s\n' "error: .venv is uv-managed; run 'make clean-venv' first"; \
		exit 1; \
	fi
	@mkdir -p $(VENV)
	@if [[ ! -f "$(VENV)/pyvenv.cfg" ]]; then \
		python3 -m venv $(VENV); \
	fi
	@$(VENV)/bin/pip install -U pip >/dev/null 2>&1
	@$(VENV)/bin/pip install . >/dev/null 2>&1
	@$(VENV)/bin/pip install -U pytest >/dev/null 2>&1
	@touch $(BACKEND_PIP)
	@touch $(STAMP_PIP)

status:
	@$(MAKE) setup-status >/dev/null
	@if [[ -e "$(BACKEND_UV)" ]]; then \
		printf '%s\n' "backend: uv"; \
	elif [[ -e "$(BACKEND_PIP)" ]]; then \
		printf '%s\n' "backend: pip"; \
	fi
	@printf '%s\n' "venv: $(VENV)"
	@printf '%s\n' "kpclientd(bin): $$HOME/.local/bin/kpclientd"
	@systemctl --user is-active kpclientd >/dev/null 2>&1 && echo "kpclientd(service): active" || echo "kpclientd(service): inactive"
	@command -v kpclientd >/dev/null 2>&1 && echo "kpclientd(path): found" || echo "kpclientd(path): missing"

code-generator: $(GEN_RES) $(GEN_UI_MIX) $(GEN_UI_FONT)

regen-code:
	@rm -f $(GEN_RES) $(GEN_UI_MIX) $(GEN_UI_FONT)
	@$(MAKE) code-generator >/dev/null

$(GEN_RES): resources/resources.qrc
	@$(MAKE) setup-status >/dev/null
	@$(VENV)/bin/pyside6-rcc resources/resources.qrc -o $(GEN_RES) >/dev/null 2>&1

$(GEN_UI_MIX): ui/mixchat.ui $(GEN_RES)
	@$(MAKE) setup-status >/dev/null
	@$(VENV)/bin/pyside6-uic --from-imports ui/mixchat.ui -o $(GEN_UI_MIX) >/dev/null 2>&1

$(GEN_UI_FONT): ui/font-settings.ui
	@$(MAKE) setup-status >/dev/null
	@$(VENV)/bin/pyside6-uic --from-imports ui/font-settings.ui -o $(GEN_UI_FONT) >/dev/null 2>&1

run: setup code-generator
	@if [[ -e "$(BACKEND_UV)" ]]; then \
		$(MAKE) run-uv; \
	elif [[ -e "$(BACKEND_PIP)" ]]; then \
		$(MAKE) run-pip; \
	else \
		printf '%s\n' "error: no backend selected. run: make setup-uv OR make setup-pip"; \
		exit 1; \
	fi

run-uv: $(STAMP_UV) code-generator
	@$(UV) run katzenqt

run-pip: $(STAMP_PIP) code-generator
	@$(VENV)/bin/katzenqt

test: setup
	@if [[ -e "$(BACKEND_UV)" ]]; then \
		$(MAKE) test-uv; \
	elif [[ -e "$(BACKEND_PIP)" ]]; then \
		$(MAKE) test-pip; \
	else \
		printf '%s\n' "error: no backend selected. run: make setup-uv OR make setup-pip"; \
		exit 1; \
	fi

test-uv: $(STAMP_UV)
	@$(UV) run pytest

test-pip: $(STAMP_PIP)
	@$(VENV)/bin/pytest

# Run the docker-integration tests. Requires a Katzenpost docker mixnet
# already running (see katzenpost-update + $(KATZENPOST_DIR)/docker: make
# start wait). Will auto-skip if kpclientd isn't reachable at
# 127.0.0.1:64331.
#
# THIN_CLIENT_DIR: optional local checkout of https://github.com/katzenpost/thin_client
# installed in editable mode so the integration tests see protocol changes
# before they are released. Set to empty to use the version pinned in
# pyproject.toml.
THIN_CLIENT_DIR ?= $(HOME)/thin_client
docker-integration: setup
	@# Install katzenqt editable so source edits land without a full
	@# `make setup` cycle. `make setup` itself copy-installs (no -e),
	@# which defeats iterating on src/katzenqt during test authoring.
	@if [[ -e "$(BACKEND_UV)" ]]; then \
		$(UV) pip install -e . >/dev/null; \
	elif [[ -e "$(BACKEND_PIP)" ]]; then \
		$(VENV)/bin/pip install -e . >/dev/null; \
	fi
	@# Install the local thin_client checkout in editable mode so the
	@# integration tests exercise protocol changes not yet released.
	@if [[ -n "$(THIN_CLIENT_DIR)" ]] && [[ -d "$(THIN_CLIENT_DIR)" ]]; then \
		if [[ -e "$(BACKEND_UV)" ]]; then \
			$(UV) pip install -e "$(THIN_CLIENT_DIR)" >/dev/null; \
		elif [[ -e "$(BACKEND_PIP)" ]]; then \
			$(VENV)/bin/pip install -e "$(THIN_CLIENT_DIR)" >/dev/null; \
		fi; \
	fi
	@# Bypass `uv run`'s lock-resync — it would re-resolve the git-pinned
	@# thinclient and silently overwrite our editable install.
	@# Run the non-container-bouncing tests in parallel (see
	@# @pytest.mark.serial_docker), then the container-bouncing ones serially
	@# afterwards: they stop/pause the shared kpclientd/gateway containers,
	@# which must not race the parallel workers. The serial phase always
	@# runs, even if the parallel phase fails, so the shared containers are
	@# still probed on every invocation (mirrors CI's `if: always()`).
	@set +e; \
	KATZENQT_DOCKER_INTEGRATION=1 $(VENV)/bin/pytest tests/integration -vv \
		-n "$(KQT_INTEGRATION_PARALLEL)" --dist loadscope -m "not serial_docker"; \
	parallel_rc=$$?; \
	KATZENQT_DOCKER_INTEGRATION=1 $(VENV)/bin/pytest tests/integration -vv -m serial_docker; \
	serial_rc=$$?; \
	set -e; \
	if [[ $$parallel_rc -ne 0 ]]; then exit $$parallel_rc; fi; \
	exit $$serial_rc
.PHONY: docker-integration

$(KATZENPOST_DIR):
	@git clone $(KATZENPOST_URL) $(KATZENPOST_DIR) >/dev/null 2>&1

katzenpost-update: $(KATZENPOST_DIR)
	@cd $(KATZENPOST_DIR) && git pull --ff-only >/dev/null 2>&1

kpclientd: $(KATZENPOST_DIR)
	@set +e; \
	( cd $(KATZENPOST_DIR)/cmd/kpclientd/ && go build -v >/dev/null 2>&1 ) ; \
	rc=$$?; \
	set -e; \
	if [[ $$rc -ne 0 ]]; then \
		printf '%s\n' "warn: native kpclientd build failed; falling back to kpclientd-podman"; \
		$(MAKE) kpclientd-podman; \
	fi

kpclientd-podman:
	@cd $(KATZENPOST_DIR)/docker && make warped=false distro=bookworm \
        voting_mixnet/kpclientd.bookworm && \
        mv voting_mixnet/kpclientd.bookworm ../cmd/kpclientd/kpclientd

# Installs the namenlos production configs: a kpclientd config that dials
# the namenlos mixnet and a thin client config that reaches the daemon over
# the @katzenpost abstract socket. The integration tests do not read these,
# dialing kpclientd directly via --address instead.
install-kpclient: kpclientd
	@install -d -m 0700 ~/.local/bin
	@install -d -m 0700 ~/.local/katzenpost/
	@install -m 0600 config/client.toml ~/.local/katzenpost/client.toml
	@install -m 0600 config/thinclient.toml ~/.local/katzenpost/thinclient.toml
	@install -m 0755 $(KATZENPOST_DIR)/cmd/kpclientd/kpclientd ~/.local/bin/kpclientd

kpclientd.service: install-kpclient
	@install -d -m 0700 ~/.config/systemd/user
	@install -m 0644 config/kpclientd.service ~/.config/systemd/user/kpclientd.service
	@systemctl --user daemon-reload
	@systemctl --user enable --now kpclientd >/dev/null 2>&1

alembic-check-uv:
	@$(UV) run alembic -c config/alembic.ini check

alembic-check-pip:
	@$(VENV)/bin/alembic -c config/alembic.ini check

alembic-revision-uv:
	@if [[ -z "$(ALEMBIC_MSG)" ]]; then \
		printf '%s\n' "error: set ALEMBIC_MSG, e.g. make $@ ALEMBIC_MSG='some change'"; \
		exit 2; \
	fi
	@$(UV) run alembic -c config/alembic.ini revision --autogenerate -m $(ALEMBIC_MSG_Q)

alembic-revision-pip:
	@if [[ -z "$(ALEMBIC_MSG)" ]]; then \
		printf '%s\n' "error: set ALEMBIC_MSG, e.g. make $@ ALEMBIC_MSG='some change'"; \
		exit 2; \
	fi
	@$(VENV)/bin/alembic -c config/alembic.ini revision --autogenerate -m $(ALEMBIC_MSG_Q)

clean-venv:
	@rm -r $(VENV)

clean:
	@rm -r $(VENV)
	@rm $(SYSTEM_STAMP)


ACT_ARGS ?=
ACT_RUNNER_IMAGE ?= ghcr.io/catthehacker/ubuntu:rust-24.04

.PHONY: ci-local
ci-local:
	@command -v act >/dev/null || { printf '%s\n' 'act is required' >&2; exit 1; }
	command -v curl >/dev/null || { printf '%s\n' 'curl is required' >&2; exit 1; }
	command -v podman >/dev/null || { printf '%s\n' 'podman is required' >&2; exit 1; }
	command -v python3 >/dev/null || { printf '%s\n' 'python3 is required' >&2; exit 1; }
	endpoint="$${DOCKER_HOST:-unix://$${XDG_RUNTIME_DIR:-/run/user/$$(id -u)}/podman/podman.sock}"
	case "$$endpoint" in
		unix:///*) socket="$${endpoint#unix://}" ;;
		*) printf '%s\n' 'ci-local requires a local Unix socket' >&2; exit 1 ;;
	esac
	if [[ ! -S "$$socket" ]]; then
		printf 'Podman socket is unavailable: %s\n' "$$socket" >&2
		printf '%s\n' 'Start it with: systemctl --user start podman.socket' >&2
		exit 1
	fi
	if ! reply=$$(curl --disable --fail --silent --show-error --max-time 5 \
		--noproxy '*' --unix-socket "$$socket" http://localhost/_ping); then
		printf 'Podman API is not responding on %s\n' "$$socket" >&2
		exit 1
	fi
	if [[ "$$reply" != OK ]]; then
		printf 'Unexpected Podman API response on %s\n' "$$socket" >&2
		exit 1
	fi
	export DOCKER_HOST="$$endpoint"
	mkdir -p "$(CURDIR)/.ci-local/uv-cache" \
		"$(CURDIR)/.ci-local/go-mod" "$(CURDIR)/.ci-local/go-build" \
		"$(CURDIR)/.ci-local/cargo-home"
	lock="$(CURDIR)/.ci-local/ci-local.lock"
	if ! mkdir "$$lock" 2>/dev/null; then
		owner=
		if [[ -r "$$lock/pid" ]]; then
			IFS= read -r owner < "$$lock/pid" || true
		fi
		if [[ "$$owner" =~ ^[0-9]+$$ ]] && kill -0 "$$owner" 2>/dev/null; then
			printf 'ci-local is already running (pid %s)\n' "$$owner" >&2
			exit 1
		fi
		rm -rf "$$lock"
		mkdir "$$lock"
	fi
	printf '%s\n' "$$$$" > "$$lock/pid"
	trap 'rm -rf "$$lock"' EXIT
	trap 'exit 130' INT
	trap 'exit 143' TERM
	stale=()
	while IFS= read -r container; do
		[[ -n "$$container" ]] || continue
		working_dir=$$(podman inspect --format \
			'{{ index .Config.Labels "com.docker.compose.project.working_dir" }}' \
			"$$container" 2>/dev/null || true)
		case "$$working_dir" in
			"$(CURDIR)/.ci-local/"*) stale+=("$$container") ;;
		esac
	done < <(podman ps -a --filter label=com.docker.compose.project \
		--format '{{.ID}}')
	if (( $${#stale[@]} )); then
		printf 'Removing stale local CI container(s): %s\n' "$${stale[*]}"
		podman rm -f -v "$${stale[@]}"
	fi
	if python3 -c 'import socket; s=socket.socket(); s.settimeout(0.2); raise SystemExit(0 if s.connect_ex(("127.0.0.1", 64331)) == 0 else 1)'; then
		printf '%s\n' 'local CI port 64331 is already in use' >&2
		podman ps --format '{{.ID}} {{.Names}} {{.Ports}}' >&2 || true
		if command -v ss >/dev/null; then
			ss -ltnp 'sport = :64331' >&2 || true
		fi
		exit 1
	fi
	if ! podman image exists "$(ACT_RUNNER_IMAGE)"; then
		podman pull "$(ACT_RUNNER_IMAGE)"
	fi
	state=$$(mktemp -d)
	podman ps -a --format '{{.ID}}' > "$$state/containers.before"
	podman volume ls --format '{{.Name}}' > "$$state/volumes.before"
	podman images --filter dangling=true --format '{{.ID}}' > "$$state/images.before"
	cleanup() {
		set +e
		podman ps -a --format '{{.ID}}' > "$$state/containers.after"
		awk 'FILENAME == ARGV[1] { seen[$$0] = 1; next } !($$0 in seen)' \
			"$$state/containers.before" "$$state/containers.after" \
			| xargs -r podman rm -f -v
		podman volume ls --format '{{.Name}}' > "$$state/volumes.after"
		awk 'FILENAME == ARGV[1] { seen[$$0] = 1; next } !($$0 in seen)' \
			"$$state/volumes.before" "$$state/volumes.after" \
			| xargs -r podman volume rm -f
		podman images --filter dangling=true --format '{{.ID}}' > "$$state/images.after"
		awk 'FILENAME == ARGV[1] { seen[$$0] = 1; next } !($$0 in seen)' \
			"$$state/images.before" "$$state/images.after" \
			| xargs -r podman image rm -f
		rm -rf "$$state" "$$lock"
	}
	trap 'status=$$?; trap - EXIT INT TERM; cleanup; exit $$status' EXIT
	trap 'exit 130' INT
	trap 'exit 143' TERM
	act --rm --pull=false --concurrent-jobs 1 --network host \
		-P ubuntu-latest=$(ACT_RUNNER_IMAGE) \
		--container-daemon-socket "$$endpoint" \
		--container-options '--volume "$(CURDIR)/.ci-local:$(CURDIR)/.ci-local"' \
		--env "UV_CACHE_DIR=$(CURDIR)/.ci-local/uv-cache" \
		--env UV_LINK_MODE=copy \
		--env "GOMODCACHE=$(CURDIR)/.ci-local/go-mod" \
		--env "GOCACHE=$(CURDIR)/.ci-local/go-build" \
		--env "CARGO_HOME=$(CURDIR)/.ci-local/cargo-home" \
		--artifact-server-path "$(CURDIR)/.ci-local/artifacts" \
		--artifact-server-addr 127.0.0.1 $(ACT_ARGS)
