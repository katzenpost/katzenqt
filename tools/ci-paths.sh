set -euo pipefail

relative=katzenqt/katzenpost
checkout_clean=true
if [[ "${ACT:-}" == true ]]; then
    relative=".ci-local/$GITHUB_JOB/katzenpost"
    checkout_clean=false
    test -S /var/run/docker.sock || {
        printf '%s\n' 'The local container socket is missing; use make ci-local' >&2
        exit 1
    }
    mountpoint -q "$GITHUB_WORKSPACE/.ci-local" || {
        printf '%s\n' 'The mixnet workspace is not shared; use make ci-local' >&2
        exit 1
    }
    printf '%s\n' 'DOCKER_HOST=unix:///var/run/docker.sock' \
        'CONTAINER_HOST=unix:///var/run/docker.sock' \
        'KATZENQT_CONTAINER_ENGINE=docker' >> "$GITHUB_ENV"
fi
printf 'KATZENQT_CHECKOUT_CLEAN=%s\n' "$checkout_clean" >> "$GITHUB_ENV"
printf 'KATZENPOST_CI_PATH=%s\n' "$relative" >> "$GITHUB_ENV"
printf 'KATZENPOST_DIR=%s/%s\n' "$GITHUB_WORKSPACE" "$relative" >> "$GITHUB_ENV"
printf 'KATZENPOST_DOCKER_COMPOSE=%s/%s/docker/mixnet-alpine/docker-compose.yml\n' \
    "$GITHUB_WORKSPACE" "$relative" >> "$GITHUB_ENV"
