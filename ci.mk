# SPDX-License-Identifier: AGPL-3.0-only

FORGEJO_RUNNER ?= forgejo-runner
WOODPECKER ?= woodpecker-cli
ci_make=$(MAKE) -f $(firstword $(MAKEFILE_LIST))
RUNNER ?=
CI_RUNNERS ?= act forgejo-runner woodpecker-cli
CI_WORKFLOWS_WOODPECKER ?= .woodpecker
CI_WOODPECKER_BACKEND ?= docker
CI_WOODPECKER_ARGS ?= --local --backend-engine $(CI_WOODPECKER_BACKEND)
CI_WORKFLOW ?=
CI_IMAGE_NAME ?= katzenqt-ci
CI_IMAGE_TAG ?= latest
CI_REGISTRIES ?=
CI_WORKFLOWS_FORGEJO ?= .forgejo/workflows
CI_FORGEJO_ARGS ?=
CI_SOCKET ?= /run/user/$(shell id -u)/podman/podman.sock
CI_DAEMON_SOCKET ?= unix://$(CI_SOCKET)
CI_WORKFLOWS_ACT ?= .github/workflows
CI_WORKFLOW_FORGEJO ?= ci.yml
CI_WORKFLOWS_WOODPECKER_DEFAULT ?= ci.yaml
CI_JOB ?=
CI_JOB_DEFAULT ?= test
CI_RUN_OPTIONS ?= --volume "$(CURDIR)/.ci-local:$(CURDIR)/.ci-local"

ACT ?= act
CONTAINER_ENGINE ?= podman
CI_IMAGE_DOCKERFILE ?= .ci/Dockerfile
CI_IMAGE_TMPDIR ?= /var/tmp
CI_IMAGE_LOCAL ?= localhost/$(CI_IMAGE_NAME):$(CI_IMAGE_TAG)
CI_IMAGE_DIGEST ?=
CI_IMAGE_PULL ?=
CI_IMAGE ?= $(if $(CI_IMAGE_DIGEST),$(CI_IMAGE_DIGEST),$(CI_IMAGE_LOCAL))
CI_LOCAL_SHELL_ARGS ?= --rm -it
ACT_ARGS ?=

.PHONY: ci-unit
ci-unit:
	uv sync --all-extras --dev --locked --python 3.12
	uv run pytest

.PHONY: check-migrations
check-migrations:
	uv run alembic -c config/alembic.ini upgrade head
	uv run alembic -c config/alembic.ini check

.PHONY: check-live
check-live:
	@host="$${KATZENQT_KPCLIENTD_HOST:-127.0.0.1}"; \
	port="$${KATZENQT_KPCLIENTD_PORT:-64331}"; \
	seen=$$($(UV) run python -c "import socket; \
s=socket.socket(); s.settimeout(5); \
print(s.connect_ex(('$$host',int('$$port'))))") \
		|| { echo "check-live: uv could not run python" >&2; exit 1; }; \
	[ "$$seen" = 0 ] \
		|| { echo "check-live: no kpclientd on $$host:$$port" >&2; exit 1; }; \
	KATZENQT_DOCKER_INTEGRATION=1 $(UV) run pytest tests/integration -q

.PHONY: ci-local-image
ci-local-image:
	@if [ -n "$(CI_IMAGE_DIGEST)" ]; then \
		$(CONTAINER_ENGINE) pull $(CI_IMAGE_DIGEST); \
		$(CONTAINER_ENGINE) tag $(CI_IMAGE_DIGEST) $(CI_IMAGE_LOCAL); \
	elif [ -n "$(CI_IMAGE_PULL)" ]; then \
		$(CONTAINER_ENGINE) pull $(CI_IMAGE_PULL); \
		$(CONTAINER_ENGINE) tag $(CI_IMAGE_PULL) $(CI_IMAGE_LOCAL); \
	else \
		TMPDIR=$(CI_IMAGE_TMPDIR) $(CONTAINER_ENGINE) build -f $(CI_IMAGE_DOCKERFILE) -t $(CI_IMAGE_LOCAL) .; \
	fi

.PHONY: ci-local-image-shell
ci-local-image-shell:
	$(CONTAINER_ENGINE) run $(CI_LOCAL_SHELL_ARGS) \
		--network host \
		--volume "$(CURDIR):$(CURDIR)" \
		--workdir "$(CURDIR)" \
		--volume "$(CI_SOCKET):$(CI_SOCKET)" \
		--env DOCKER_HOST="$(CI_DAEMON_SOCKET)" \
		--entrypoint /bin/bash \
		$(CI_IMAGE_LOCAL)

.PHONY: ci-local-act
ci-local-act:
	@command -v "$(ACT)" >/dev/null || { printf '%s\n' '$(ACT) is required' >&2; exit 1; }
	command -v curl >/dev/null || { printf '%s\n' 'curl is required' >&2; exit 1; }
	command -v $(CONTAINER_ENGINE) >/dev/null || { printf '%s\n' '$(CONTAINER_ENGINE) is required' >&2; exit 1; }
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
	tmp="$(CURDIR)/.ci-local/tmp"
	rm -rf "$$tmp"
	mkdir -p "$$tmp"
	export TMPDIR="$$tmp"
	stale=()
	while IFS= read -r container; do
		[[ -n "$$container" ]] || continue
		working_dir=$$($(CONTAINER_ENGINE) inspect --format \
			'{{ index .Config.Labels "com.docker.compose.project.working_dir" }}' \
			"$$container" 2>/dev/null || true)
		case "$$working_dir" in
			"$(CURDIR)/.ci-local/"*) stale+=("$$container") ;;
		esac
	done < <($(CONTAINER_ENGINE) ps -a --filter label=com.docker.compose.project \
		--format '{{.ID}}')
	if (( $${#stale[@]} )); then
		printf 'Removing stale local CI container(s): %s\n' "$${stale[*]}"
		$(CONTAINER_ENGINE) rm -f -v "$${stale[@]}"
	fi
	if python3 -c 'import socket; s=socket.socket(); s.settimeout(0.2); raise SystemExit(0 if s.connect_ex(("127.0.0.1", 64331)) == 0 else 1)'; then
		printf '%s\n' 'local CI port 64331 is already in use' >&2
		$(CONTAINER_ENGINE) ps --format '{{.ID}} {{.Names}} {{.Ports}}' >&2 || true
		if command -v ss >/dev/null; then
			ss -ltnp 'sport = :64331' >&2 || true
		fi
		exit 1
	fi
	state=$$(mktemp -d "$$tmp/state.XXXXXXXXXX")
	$(CONTAINER_ENGINE) ps -a --format '{{.ID}}' > "$$state/containers.before"
	$(CONTAINER_ENGINE) volume ls --format '{{.Name}}' > "$$state/volumes.before"
	$(CONTAINER_ENGINE) images --filter dangling=true --format '{{.ID}}' > "$$state/images.before"
	cleanup() {
		set +e
		$(CONTAINER_ENGINE) ps -a --format '{{.ID}}' > "$$state/containers.after"
		awk 'FILENAME == ARGV[1] { seen[$$0] = 1; next } !($$0 in seen)' \
			"$$state/containers.before" "$$state/containers.after" \
			| xargs -r $(CONTAINER_ENGINE) rm -f -v
		$(CONTAINER_ENGINE) volume ls --format '{{.Name}}' > "$$state/volumes.after"
		awk 'FILENAME == ARGV[1] { seen[$$0] = 1; next } !($$0 in seen)' \
			"$$state/volumes.before" "$$state/volumes.after" \
			| xargs -r $(CONTAINER_ENGINE) volume rm -f
		$(CONTAINER_ENGINE) images --filter dangling=true --format '{{.ID}}' > "$$state/images.after"
		awk 'FILENAME == ARGV[1] { seen[$$0] = 1; next } !($$0 in seen)' \
			"$$state/images.before" "$$state/images.after" \
			| xargs -r $(CONTAINER_ENGINE) image rm -f
		rm -rf "$$state" "$$lock" "$$tmp"
	}
	trap 'status=$$?; trap - EXIT INT TERM; cleanup; exit $$status' EXIT
	trap 'exit 130' INT
	trap 'exit 143' TERM
	"$(ACT)" -P "ubuntu-24.04=$(CI_IMAGE)" --rm --concurrent-jobs 1 --network host \
		-P ubuntu-latest=$(CI_IMAGE) --pull=false --var CI_IMAGE=$(CI_IMAGE) \
		--container-daemon-socket "$$endpoint" \
		--container-options '--volume "$(CURDIR)/.ci-local:$(CURDIR)/.ci-local"' \
		--env "UV_CACHE_DIR=$(CURDIR)/.ci-local/uv-cache" \
		--env UV_LINK_MODE=copy \
		--env "GOMODCACHE=$(CURDIR)/.ci-local/go-mod" \
		--env "GOCACHE=$(CURDIR)/.ci-local/go-build" \
		--env "CARGO_HOME=$(CURDIR)/.ci-local/cargo-home" \
		--artifact-server-path "$(CURDIR)/.ci-local/artifacts" \
		--artifact-server-addr 127.0.0.1 \
		$(if $(CI_WORKFLOW),-W $(CI_WORKFLOWS_ACT)/$(CI_WORKFLOW),) \
		-j $(if $(CI_JOB),$(CI_JOB),$(CI_JOB_DEFAULT)) $(ACT_ARGS)

.PHONY: ci-local-image-push
ci-local-image-push: ci-local-image
	@test -n "$(CI_REGISTRIES)" || { printf '%s\n' 'set CI_REGISTRIES to one or more registry prefixes' >&2; exit 1; }
	@set -e; for registry in $(CI_REGISTRIES); do \
		$(CONTAINER_ENGINE) tag $(CI_IMAGE_LOCAL) $$registry/$(CI_IMAGE_NAME):$(CI_IMAGE_TAG); \
		$(CONTAINER_ENGINE) push $$registry/$(CI_IMAGE_NAME):$(CI_IMAGE_TAG); \
	done

.PHONY: ci-local-forgejo
ci-local-forgejo:
	@command -v "$(FORGEJO_RUNNER)" >/dev/null || { printf '%s\n' '$(FORGEJO_RUNNER) is required' >&2; exit 1; }
	DOCKER_HOST="unix://$(CI_SOCKET)" "$(FORGEJO_RUNNER)" exec $(CI_FORGEJO_ARGS) \
		-i $(CI_IMAGE) --var CI_IMAGE=$(CI_IMAGE) \
		--container-daemon-socket "unix://$(CI_SOCKET)" \
		--container-opts '$(CI_RUN_OPTIONS)' \
		-W $(CI_WORKFLOWS_FORGEJO)/$(if $(CI_WORKFLOW),$(CI_WORKFLOW),$(CI_WORKFLOW_FORGEJO)) \
		$(if $(CI_JOB),-j $(CI_JOB),)

.PHONY: ci-local
ci-local: ci-local-image
	@runner="$(RUNNER)"; \
	if [ -z "$$runner" ]; then \
		for candidate in $(CI_RUNNERS); do \
			command -v "$$candidate" >/dev/null 2>&1 && { runner="$$candidate"; break; }; \
		done; \
	fi; \
	case "$$runner" in \
		act) $(ci_make) ci-local-act;; \
		forgejo|forgejo-runner) $(ci_make) ci-local-forgejo;; \
		woodpecker|woodpecker-cli) $(ci_make) ci-local-woodpecker;; \
		"") printf '%s\n' 'no ci runner on this host; using the one in $(CI_IMAGE)'; \
		  exec $(CONTAINER_ENGINE) run --rm --network host \
		    --volume "$(CURDIR):$(CURDIR)" --workdir "$(CURDIR)" \
		    --volume "$(CI_SOCKET):$(CI_SOCKET)" \
		    --env DOCKER_HOST="$(CI_DAEMON_SOCKET)" \
		    --entrypoint /bin/bash $(CI_IMAGE) -lc \
		    'make ci-local-act CONTAINER_ENGINE=docker CI_WORKFLOW=$(CI_WORKFLOW) CI_JOB=$(CI_JOB) CI_SOCKET=$(CI_SOCKET)';; \
		*) printf '%s\n' 'RUNNER must be act, forgejo or woodpecker' >&2; exit 1;; \
	esac

.PHONY: ci-local-woodpecker
ci-local-woodpecker:
	@command -v "$(WOODPECKER)" >/dev/null || { printf '%s\n' '$(WOODPECKER) is required' >&2; exit 1; }
	@set -e; for pipeline in $(if $(CI_WORKFLOW),$(CI_WORKFLOWS_WOODPECKER)/$(CI_WORKFLOW),$(addprefix $(CI_WORKFLOWS_WOODPECKER)/,$(CI_WORKFLOWS_WOODPECKER_DEFAULT))); do \
		DOCKER_HOST="unix://$(CI_SOCKET)" "$(WOODPECKER)" exec $(CI_WOODPECKER_ARGS) \
			--repo-path "$(CURDIR)" "$$pipeline"; done

.PHONY: ruff ruff-uv ruff-pip
ruff: setup
	@if [[ -e "$(BACKEND_UV)" ]]; then \
		$(ci_make) ruff-uv; \
	elif [[ -e "$(BACKEND_PIP)" ]]; then \
		$(ci_make) ruff-pip; \
	else \
		printf '%s\n' "error: no backend selected"; \
		printf '%s\n' "run: make setup-uv OR make setup-pip"; \
		exit 1; \
	fi

ruff-uv:
	@$(UV) run ruff check src tests
	@$(UV) run ruff format --check src tests

ruff-pip: setup
	@$(VENV)/bin/ruff check src tests
	@$(VENV)/bin/ruff format --check src tests
