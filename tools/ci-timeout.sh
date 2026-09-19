set -euo pipefail

case "${KQT_CI_DEADLINE:-}" in
    ''|*[!0-9]*) printf '%s\n' 'A CI deadline is required' >&2; exit 2 ;;
esac
remaining=$((KQT_CI_DEADLINE - $(date +%s)))
if ((remaining <= 0)); then
    printf '%s\n' 'The CI job exhausted its existing time budget' >&2
    exit 124
fi
exec timeout --kill-after=10s "${remaining}s" "$@"
