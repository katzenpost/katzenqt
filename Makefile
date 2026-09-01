SHELL := /bin/bash
.ONESHELL:
.SHELLFLAGS := -eu -o pipefail -c

.DEFAULT_GOAL := help
MAKEFLAGS += --no-print-directory

export PATH:=$(PATH):~/.local/bin/
export UV_VENV_CLEAR:=1
export GOCACHE:=$(CURDIR)/.go-cache
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
# Branch tip: the --dbus-name + multi-unix-listener fix is not yet on
# katzenpost main, so we pin the exact commit until it merges.
KATZENPOST_REV := 3b0e511ea64690070a583484a04b58681ca3eb12

GEN_RES := src/katzenqt/resources_rc.py
GEN_UI_MIX := src/katzenqt/ui_mixchat.py
GEN_UI_FONT := src/katzenqt/ui_font_settings.py

PYPROJECT := pyproject.toml
UV_LOCK := $(wildcard uv.lock)

FLATPAK_ID := network.katzenpost.katzenqt
FLATPAK_MANIFEST := packaging/flatpak/$(FLATPAK_ID).yaml
FLATPAK_EPOCH := 1787647836
FLATPAK_TIMESTAMP := 2026-08-25T08:50:36Z
FLATPAK_REPO := .flatpak-repo
FLATPAK_EXPORT := .flatpak-export
FLATPAK_SCREENSHOT := packaging/flatpak/screenshots/katzenqt.png
FLATPAK_SCREENSHOT_URL := https://raw.githubusercontent.com/katzenpost/katzenqt/main/packaging/flatpak/screenshots/katzenqt.png
FLATPAK_MEDIA_URL := https://dl.flathub.org/media/network/katzenpost/katzenqt/katzenqt.png
FLATPAK_SECOND_STATE ?= katzen-second
FLATPAK_DOCKER_ADDRESS ?= 127.0.0.1:64331
FLATHUB_DIST := .flathub-dist

# make alembic-revision-uv ALEMBIC_MSG='some changeset details'
ALEMBIC_MSG ?=
ALEMBIC_MSG_Q := "$(ALEMBIC_MSG)"

.PHONY: default default_uv_setup default_pip_setup help \
	system-setup install-debian-packages install-uv clean-system-stamp \
	setup setup-uv setup-pip setup-status \
	run test status code-generator regen-code \
	run-uv run-pip test-uv test-pip \
	alembic-check-uv alembic-check-pip \
	alembic-revision-uv alembic-revision-pip \
	katzenpost-update kpclientd kpclientd-podman install-kpclient kpclientd.service \
	flatpak-install-system-deps flatpak-runtime flatpak-not-running flatpak flatpak-build flatpak-install flatpak-run flatpak-run-second \
	flatpak-docker-check flatpak-docker-existing-check flatpak-docker-start flatpak-run-docker flatpak-run-docker-second flatpak-test flatpak-daemon-status \
	flatpak-validate flatpak-lint-runtime flatpak-lint flatpak-permissions flatpak-reproducible flatpak-test-docker \
	flathub-validate flathub-dist flathub-check flathub-submit \
	test-extensive flatpak-clean clean clean-venv deps

deps: default_uv_setup

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
		'  make katzenpost-update     Restore the pinned ignored Katzenpost checkout' \
		'  make kpclientd             Build kpclientd (golang native build; falls back to podman)' \
		'  make kpclientd-podman      Build kpclientd using the container toolchain' \
		'  make install-kpclient      Install kpclientd to ~/.local/bin/kpclientd' \
		'  make kpclientd.service     Install and enable user systemd service for kpclientd' \
		'' \
		'Flatpak:' \
		'  make flatpak-install-system-deps Install Flatpak build tools (Debian/Ubuntu)' \
		'  make flatpak-runtime       Install the GNOME 50 SDK and runtime' \
		'  make flatpak               Runtime deps, build, then install (meta)' \
		'  make flatpak-build         Build the Flatpak into a local repo (no install)' \
		'  make flatpak-install       Install the already-built local repo' \
		'  make flatpak-run           Run the installed Flatpak' \
		'  make flatpak-run-second    Run a second Flatpak identity' \
		'  make flatpak-run-docker    Run the first identity on the Docker testnet' \
		'  make flatpak-run-docker-second Run the second identity on the Docker testnet' \
		'  make flatpak-docker-start  Start and wait for the local Docker testnet' \
		'  make flatpak-test          Test imports, migrations, and daemon configuration' \
		'  make flatpak-daemon-status Show the selected Flatpak daemon' \
		'  make flatpak-validate      Validate the application metadata' \
		'  make flatpak-lint          Run Flathub manifest and repository lint' \
		'  make flatpak-permissions   Verify the installed sandbox policy' \
		'  make flatpak-reproducible  Compare two clean Flatpak exports' \
		'  make flatpak-test-docker   Run the real-network integration suite' \
		'  make test-extensive        Run the complete release gate' \
		'  make flathub-dist TAG=v0.0.1 Create a tagged Flathub submission tree' \
		'  make flathub-check TAG=v0.0.1 Test a tagged Flathub release' \
		'  make flathub-submit TAG=v0.0.1 Open or update the Flathub pull request' \
		'  make flatpak-clean         Remove local Flatpak build output' \
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
	@KATZENQT_GUI=$(CURDIR)/$(VENV)/bin/katzenqt $(VENV)/bin/python -m katzenqt.launcher

run-pip: $(STAMP_PIP) code-generator
	@KATZENQT_GUI=$(CURDIR)/$(VENV)/bin/katzenqt $(VENV)/bin/python -m katzenqt.launcher

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
	@env -u KATZENQT_DOCKER_INTEGRATION $(UV) run pytest -m "not integration"

test-pip: $(STAMP_PIP)
	@env -u KATZENQT_DOCKER_INTEGRATION $(VENV)/bin/pytest -m "not integration"

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
	@KATZENQT_DOCKER_INTEGRATION=1 $(VENV)/bin/pytest tests/integration -vv
.PHONY: docker-integration

$(KATZENPOST_DIR):
	@git clone $(KATZENPOST_URL) $(KATZENPOST_DIR) >/dev/null 2>&1
	@git -C $(KATZENPOST_DIR) switch --detach $(KATZENPOST_REV) >/dev/null 2>&1

katzenpost-update: $(KATZENPOST_DIR)
	@git -C $(KATZENPOST_DIR) fetch origin $(KATZENPOST_REV) >/dev/null 2>&1
	@git -C $(KATZENPOST_DIR) switch --detach $(KATZENPOST_REV) >/dev/null 2>&1

kpclientd: $(KATZENPOST_DIR)
	@test -z "$$(git -C $(KATZENPOST_DIR) status --porcelain)" || { printf '%s\n' 'error: katzenpost checkout is dirty; run make katzenpost-update'; exit 1; }
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
	@install -m 0600 src/katzenqt/data/client.toml ~/.local/katzenpost/client.toml
	@install -m 0600 src/katzenqt/data/thinclient.toml ~/.local/katzenpost/thinclient.toml
	@install -m 0755 $(KATZENPOST_DIR)/cmd/kpclientd/kpclientd ~/.local/bin/kpclientd

# Install + start the user service via the shared launcher code (single
# implementation, also used by the non-Flatpak runtime fallback).
kpclientd.service: install-kpclient
	@$(UV) run python -m katzenqt.launcher --install-service

flatpak-install-system-deps:
	@sudo apt install -y appstream flatpak flatpak-builder git-lfs

flatpak-runtime:
	@flatpak install --user -y flathub org.gnome.Platform//50 org.gnome.Sdk//50

flatpak-lint-runtime:
	@flatpak install --user -y flathub org.flatpak.Builder

flatpak-not-running:
	@for _ in {1..50}; do \
		if ! flatpak ps --columns=application | grep -Fxq $(FLATPAK_ID); then exit 0; fi; \
		sleep .1; \
	done; \
	printf '%s\n' 'Close all running katzenqt Flatpak clients before rebuilding.'; \
	exit 1

# Build the Flatpak into a local ostree repo; does not install anything.
flatpak-build: flatpak-runtime
	@rm -rf $(FLATPAK_REPO) $(FLATPAK_EXPORT)
	@flatpak-builder --force-clean --override-source-date-epoch=$(FLATPAK_EPOCH) --repo=$(FLATPAK_EXPORT) .flatpak-build $(FLATPAK_MANIFEST)
	@python3 packaging/flatpak/mirror-screenshot.py catalog .flatpak-build $(FLATPAK_SCREENSHOT) $(FLATPAK_MEDIA_URL) $(FLATPAK_TIMESTAMP)
	@flatpak build-export --update-appstream --timestamp=$(FLATPAK_TIMESTAMP) $(FLATPAK_REPO) .flatpak-build master
	@python3 packaging/flatpak/mirror-screenshot.py repo $(FLATPAK_REPO) $(FLATPAK_SCREENSHOT) $(FLATPAK_MEDIA_URL) $(FLATPAK_TIMESTAMP)
	@flatpak build-update-repo --no-update-appstream $(FLATPAK_REPO)

# Install the already-built local repo for this user.
flatpak-install: flatpak-not-running
	@flatpak install --user --reinstall -y $(CURDIR)/$(FLATPAK_REPO) $(FLATPAK_ID)

# Meta-target: install runtime deps, build, then install.
flatpak: flatpak-runtime flatpak-not-running flatpak-build flatpak-install

flatpak-run:
	@flatpak run $(FLATPAK_ID)

flatpak-run-second:
	@flatpak run --env=KQT_STATE=$(FLATPAK_SECOND_STATE) $(FLATPAK_ID)

flatpak-docker-check:
	@python3 -c 'import socket; socket.create_connection(("$(word 1,$(subst :, ,$(FLATPAK_DOCKER_ADDRESS)))", $(word 2,$(subst :, ,$(FLATPAK_DOCKER_ADDRESS)))), 1).close()' 2>/dev/null || { printf '%s\n' 'Docker kpclientd is unavailable at $(FLATPAK_DOCKER_ADDRESS).' 'Start it with: cd katzenpost/docker && make start wait'; exit 1; }

flatpak-docker-existing-check: flatpak-docker-check
	@test -f $(KATZENPOST_DIR)/docker/voting_mixnet/running.stamp
	@$(MAKE) -C $(KATZENPOST_DIR)/docker ps | grep -Eq '(^|[[:space:]])kpclientd([[:space:]]|$$)'

flatpak-docker-start: $(KATZENPOST_DIR)
	@$(MAKE) -C $(KATZENPOST_DIR)/docker start wait
	@$(MAKE) flatpak-docker-check

flatpak-run-docker: flatpak-docker-check
	@flatpak run --share=network --env=KQT_STATE=katzen-docker --env=KATZENQT_KPCLIENTD_TCP=$(FLATPAK_DOCKER_ADDRESS) $(FLATPAK_ID)

flatpak-run-docker-second: flatpak-docker-check
	@flatpak run --share=network --env=KQT_STATE=katzen-docker-second --env=KATZENQT_KPCLIENTD_TCP=$(FLATPAK_DOCKER_ADDRESS) $(FLATPAK_ID)

flatpak-test:
	@flatpak run --command=python3 $(FLATPAK_ID) -c \
		'import importlib.resources; import katzenqt, katzenpost_thinclient; assert (importlib.resources.files("katzenqt") / "migrations").is_dir()'
	@flatpak run --command=python3 $(FLATPAK_ID) -c \
		'import configparser; c=configparser.ConfigParser(); c.read("/.flatpak-info"); assert "network" not in c.get("Context", "shared", fallback="").split(";")'
	@flatpak run --command=sh $(FLATPAK_ID) -c 'test ! -e /app/bin/kpclientd'
	@flatpak run --command=python3 --env=KQT_STATE=flatpak-test-a $(FLATPAK_ID) -c \
		'from katzenqt.persistent import state_file; assert state_file.name == "flatpak-test-a.sqlite3"; state_file.touch()'
	@flatpak run --command=python3 --env=KQT_STATE=flatpak-test-b $(FLATPAK_ID) -c \
		'from katzenqt.persistent import state_file; assert state_file.name == "flatpak-test-b.sqlite3"; assert state_file.with_name("flatpak-test-a.sqlite3") != state_file'

flatpak-daemon-status:
	@flatpak run $(FLATPAK_ID) --status

flatpak-validate:
	@appstreamcli validate --no-net packaging/flatpak/$(FLATPAK_ID).metainfo.xml

flatpak-lint: flatpak-lint-runtime
	@if command -v flatpak-builder-lint >/dev/null 2>&1; then \
		flatpak-builder-lint manifest $(FLATPAK_MANIFEST); \
		flatpak-builder-lint repo $(FLATPAK_REPO); \
	elif flatpak info org.flatpak.Builder >/dev/null 2>&1; then \
		flatpak run --filesystem=$(CURDIR) --command=flatpak-builder-lint org.flatpak.Builder manifest $(CURDIR)/$(FLATPAK_MANIFEST); \
		flatpak run --filesystem=$(CURDIR) --command=flatpak-builder-lint org.flatpak.Builder repo $(CURDIR)/$(FLATPAK_REPO); \
	else \
		printf '%s\n' 'flatpak-builder-lint is unavailable; install it or org.flatpak.Builder'; \
		exit 1; \
	fi

flatpak-permissions:
	@flatpak run --command=python3 $(FLATPAK_ID) -c \
		'import configparser; c=configparser.ConfigParser(); c.read("/.flatpak-info"); shared=c.get("Context", "shared", fallback="").split(";"); files=c.get("Context", "filesystems", fallback="").split(";"); assert "network" not in shared; assert files == ["xdg-run/katzenpost:ro", ""]'
	@flatpak run --command=python3 $(FLATPAK_ID) -c \
		'import socket; s=socket.socket(); s.settimeout(.2); r=s.connect_ex(("1.1.1.1", 53)); assert r != 0, r'

flatpak-reproducible: flatpak
	@first=$$(ostree refs --repo=$(FLATPAK_REPO) | sort | while read ref; do printf '%s %s\n' "$$ref" "$$(ostree --repo=$(FLATPAK_REPO) rev-parse "$$ref")"; done); \
	$(MAKE) flatpak >/dev/null; \
	second=$$(ostree refs --repo=$(FLATPAK_REPO) | sort | while read ref; do printf '%s %s\n' "$$ref" "$$(ostree --repo=$(FLATPAK_REPO) rev-parse "$$ref")"; done); \
	test "$$first" = "$$second"; \
	printf '%s\n' "$$second"

flatpak-test-docker: $(KATZENPOST_DIR)
	@started=; \
	cleanup() { if [[ -n "$$started" ]]; then $(MAKE) -C $(KATZENPOST_DIR)/docker stop; fi; }; \
	trap cleanup EXIT INT TERM; \
	if $(MAKE) flatpak-docker-check >/dev/null 2>&1; then \
		$(MAKE) flatpak-docker-existing-check; \
	else \
		started=1; \
		$(MAKE) -C $(KATZENPOST_DIR)/docker start wait; \
	fi; \
	$(MAKE) setup-uv; \
	$(MAKE) flatpak-docker-check; \
	KATZENQT_INTEGRATION_PYTHON=$(CURDIR)/packaging/flatpak/integration-python packaging/flatpak/wait-for-mixnet; \
	KATZENQT_DOCKER_INTEGRATION=1 KATZENQT_INTEGRATION_PYTHON=$(CURDIR)/packaging/flatpak/integration-python $(UV) run pytest --no-cov tests/integration; \
	if [[ -n "$$started" ]]; then \
		cleanup; \
		started=; \
		if $(MAKE) flatpak-docker-check >/dev/null 2>&1; then printf '%s\n' 'error: managed test mixnet is still reachable'; exit 1; fi; \
	fi

test-extensive: setup-uv test-uv alembic-check-uv flatpak-validate flatpak flatpak-test flatpak-permissions flatpak-reproducible flatpak-lint flatpak-test-docker

flathub-validate:
	@test -n "$(TAG)" || { printf '%s\n' 'error: TAG=MAJOR.MINOR.PATCH is required'; exit 1; }
	@python3 packaging/flatpak/release.py validate --tag "$(TAG)" --remote

flathub-dist: flathub-validate
	@python3 packaging/flatpak/release.py dist --tag "$(TAG)" --destination $(FLATHUB_DIST) --remote

flathub-check: flathub-dist test-extensive
	@cd $(FLATHUB_DIST) && flatpak run --filesystem=$(CURDIR) --command=flathub-build org.flatpak.Builder --install $(FLATPAK_ID).yaml
	@cd $(FLATHUB_DIST) && flatpak run --filesystem=$(CURDIR) --command=flatpak-builder-lint org.flatpak.Builder manifest $(FLATPAK_ID).yaml
	@cd $(FLATHUB_DIST) && flatpak run --filesystem=$(CURDIR) --command=flatpak-builder-lint org.flatpak.Builder repo repo
	@printf '%s\n' "$(TAG)" > $(FLATHUB_DIST)/.checked-tag

flathub-submit: flathub-check
	@test "$$(cat $(FLATHUB_DIST)/.checked-tag)" = "$(TAG)"
	@python3 packaging/flatpak/release.py submit --tag "$(TAG)" --destination $(FLATHUB_DIST)

flatpak-clean:
	@rm -rf .flatpak-build .flatpak-builder $(FLATPAK_REPO) $(FLATPAK_EXPORT) $(FLATHUB_DIST)

alembic-check-uv:
	@state=$$(mktemp -d); \
	trap 'rm -rf "$$state"' EXIT; \
	XDG_DATA_HOME=$$state $(UV) run alembic -c src/katzenqt/data/alembic.ini upgrade head; \
	XDG_DATA_HOME=$$state $(UV) run alembic -c src/katzenqt/data/alembic.ini check

alembic-check-pip:
	@state=$$(mktemp -d); \
	trap 'rm -rf "$$state"' EXIT; \
	XDG_DATA_HOME=$$state $(VENV)/bin/alembic -c src/katzenqt/data/alembic.ini upgrade head; \
	XDG_DATA_HOME=$$state $(VENV)/bin/alembic -c src/katzenqt/data/alembic.ini check

alembic-revision-uv:
	@if [[ -z "$(ALEMBIC_MSG)" ]]; then \
		printf '%s\n' "error: set ALEMBIC_MSG, e.g. make $@ ALEMBIC_MSG='some change'"; \
		exit 2; \
	fi
	@$(UV) run alembic -c src/katzenqt/data/alembic.ini revision --autogenerate -m $(ALEMBIC_MSG_Q)

alembic-revision-pip:
	@if [[ -z "$(ALEMBIC_MSG)" ]]; then \
		printf '%s\n' "error: set ALEMBIC_MSG, e.g. make $@ ALEMBIC_MSG='some change'"; \
		exit 2; \
	fi
	@$(VENV)/bin/alembic -c src/katzenqt/data/alembic.ini revision --autogenerate -m $(ALEMBIC_MSG_Q)

clean-venv:
	@rm -r $(VENV)

clean:
	@rm -r $(VENV)
	@rm $(SYSTEM_STAMP)
