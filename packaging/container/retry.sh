retry() {
	attempt=1
	until "$@"; do
		if [ "$attempt" -ge 3 ]; then
			printf 'giving up after %s attempts: %s\n' \
				"$attempt" "$1" >&2
			return 1
		fi
		printf 'attempt %s failed, retrying: %s\n' "$attempt" "$1" >&2
		sleep $((attempt * 5))
		attempt=$((attempt + 1))
	done
}
