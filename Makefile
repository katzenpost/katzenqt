SHELL := /bin/bash
.ONESHELL:
.SHELLFLAGS := -eu -o pipefail -c

.DEFAULT_GOAL := help
MAKEFLAGS += --no-print-directory

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

.PHONY: default default_uv_setup default_pip_setup help \
	system-setup install-debian-packages install-uv clean-system-stamp \
	setup setup-uv setup-pip setup-status \
	run test status code-generator regen-code \
	run-uv run-pip test-uv test-pip \
	alembic-check-uv alembic-check-pip \
	alembic-revision-uv alembic-revision-pip \
	katzenpost-update kpclientd install-kpclient kpclientd.service \
	clean clean-venv deps

deps: default_uv_setup

default: default_uv_setup

default_uv_setup: system-setup setup-uv setup test kpclientd install-kpclient \
		kpclientd.service status
default_pip_setup: system-setup setup-pip setup test kpclientd install-kpclient \
		kpclientd.service status

help:
	@printf '%s\n' \
		'If in doubt, run `make deps` and `source ~/.profile` and `make run`' \
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
		'  make kpclientd             Build kpclientd from ./katzenpost' \
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
		libxcb-cursor0 libegl1 \
		build-essential pkg-config \
		golang-go git \
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
	@mkdir -p $(VENV)
	@command -v "$(UV)" >/dev/null 2>&1 || { printf '%s\n' "error: uv not found (set UV=/path/to/uv)"; exit 1; }
	@if [[ ! -f "$(VENV)/pyvenv.cfg" ]]; then \
		$(UV) venv >/dev/null 2>&1; \
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

$(KATZENPOST_DIR):
	@git clone $(KATZENPOST_URL) $(KATZENPOST_DIR) >/dev/null 2>&1

katzenpost-update: $(KATZENPOST_DIR)
	@cd $(KATZENPOST_DIR) && git pull --ff-only >/dev/null 2>&1

kpclientd: $(KATZENPOST_DIR)
	@cd $(KATZENPOST_DIR)/cmd/kpclientd/ && go build -v >/dev/null 2>&1

install-kpclient: kpclientd
	@install -d -m 0700 ~/.local/bin
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

