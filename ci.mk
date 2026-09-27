# SPDX-License-Identifier: AGPL-3.0-only

FORGEJO_RUNNER ?= forgejo-runner
WOODPECKER ?= woodpecker-cli
ci_make=$(MAKE) -f $(firstword $(MAKEFILE_LIST))
RUNNER ?=
CI_RUNNERS ?= act forgejo-runner woodpecker-cli
CI_WORKFLOWS_WOODPECKER ?= .woodpecker
CI_IMAGE_NAME ?= katzenqt-ci
CI_IMAGE_TAG ?= latest
CI_REGISTRIES ?=
CI_WORKFLOWS_FORGEJO ?= .forgejo/workflows
CI_FORGEJO_ARGS ?= --bind
CI_RUN_OPTIONS ?= --volume "$(CURDIR)/.ci-local:$(CURDIR)/.ci-local"

ACT ?= act
CONTAINER_ENGINE ?= podman
CI_LOCAL_IMAGE ?= localhost/katzenqt-act:latest
CI_LOCAL_SHELL_ARGS ?= --rm -it
ACT_ARGS ?=

.PHONY: check-migrations
check-migrations:
	uv run alembic -c config/alembic.ini upgrade head
	uv run alembic -c config/alembic.ini check

.PHONY: check-live
check-live:
	python3 -m pytest tests/integration -q

.PHONY: ci-local-image
ci-local-image:
	$(CONTAINER_ENGINE) build \
		-f .github/act/Dockerfile \
		-t $(CI_LOCAL_IMAGE) \
		.

.PHONY: ci-local-image-shell
ci-local-image-shell:
	$(CONTAINER_ENGINE) run $(CI_LOCAL_SHELL_ARGS) \
		--network host \
		--volume "$(CURDIR):$(CURDIR)" \
		--workdir "$(CURDIR)" \
		--entrypoint /bin/bash \
		$(CI_LOCAL_IMAGE)

.PHONY: ci-local-act
ci-local-act:
	@command -v "$(ACT)" >/dev/null || { printf '%s\n' '$(ACT) is required' >&2; exit 1; }
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
	tmp="$(CURDIR)/.ci-local/tmp"
	rm -rf "$$tmp"
	mkdir -p "$$tmp"
	export TMPDIR="$$tmp"
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
	state=$$(mktemp -d "$$tmp/state.XXXXXXXXXX")
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
		rm -rf "$$state" "$$lock" "$$tmp"
	}
	trap 'status=$$?; trap - EXIT INT TERM; cleanup; exit $$status' EXIT
	trap 'exit 130' INT
	trap 'exit 143' TERM
	"$(ACT)" -P "ubuntu-24.04=$(CI_LOCAL_IMAGE)" --rm --concurrent-jobs 1 --network host \
		-P ubuntu-latest=$(CI_LOCAL_IMAGE) \
		--container-daemon-socket "$$endpoint" \
		--container-options '--volume "$(CURDIR)/.ci-local:$(CURDIR)/.ci-local"' \
		--env "UV_CACHE_DIR=$(CURDIR)/.ci-local/uv-cache" \
		--env UV_LINK_MODE=copy \
		--env "GOMODCACHE=$(CURDIR)/.ci-local/go-mod" \
		--env "GOCACHE=$(CURDIR)/.ci-local/go-build" \
		--env "CARGO_HOME=$(CURDIR)/.ci-local/cargo-home" \
		--artifact-server-path "$(CURDIR)/.ci-local/artifacts" \
		--artifact-server-addr 127.0.0.1 $(ACT_ARGS)

.PHONY: ci-local-image-push
ci-local-image-push: ci-local-image
	@test -n "$(CI_REGISTRIES)" || { printf '%s\n' 'set CI_REGISTRIES to one or more registry prefixes' >&2; exit 1; }
	@set -e; for registry in $(CI_REGISTRIES); do \
		$(CONTAINER_ENGINE) tag $(CI_LOCAL_IMAGE) $$registry/$(CI_IMAGE_NAME):$(CI_IMAGE_TAG); \
		$(CONTAINER_ENGINE) push $$registry/$(CI_IMAGE_NAME):$(CI_IMAGE_TAG); \
	done

.PHONY: ci-local-forgejo
ci-local-forgejo: ci-local-image
	@command -v "$(FORGEJO_RUNNER)" >/dev/null || { printf '%s\n' '$(FORGEJO_RUNNER) is required' >&2; exit 1; }
	"$(FORGEJO_RUNNER)" exec $(CI_FORGEJO_ARGS) -P "ubuntu-latest=$(CI_LOCAL_IMAGE)" \
		--container-options '$(CI_RUN_OPTIONS)' -W $(CI_WORKFLOWS_FORGEJO)

.PHONY: ci-local
ci-local:
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
		"") printf '%s\n' 'no local ci runner found; install one of: $(CI_RUNNERS)' >&2; exit 1;; \
		*) printf '%s\n' 'RUNNER must be act, forgejo or woodpecker' >&2; exit 1;; \
	esac

.PHONY: ci-local-woodpecker
ci-local-woodpecker:
	@command -v "$(WOODPECKER)" >/dev/null || { printf '%s\n' '$(WOODPECKER) is required' >&2; exit 1; }
	"$(WOODPECKER)" exec $(CI_WORKFLOWS_WOODPECKER)/ci.yaml
