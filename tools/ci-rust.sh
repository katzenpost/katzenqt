set -euo pipefail

: "${GITHUB_ENV:?}" "${GITHUB_PATH:?}" "${RUNNER_TEMP:?}"
version=1.89
apt_options=(
    -o Acquire::Retries=1
    -o Acquire::http::Timeout=30
    -o Acquire::https::Timeout=30
    -o DPkg::Lock::Timeout=30
    -o Dpkg::Use-Pty=0
)

version_ok()
{
    local binary=$1
    local actual
    actual=$($binary --version | awk '{print $2}')
    [[ "$(printf '%s\n%s\n' "$version" "$actual" | sort -V | head -n1)" == "$version" ]]
}

cargo=$(command -v cargo || true)
rustc=$(command -v rustc || true)
if [[ -z "$cargo" || -z "$rustc" ]] || ! version_ok "$cargo" || ! version_ok "$rustc"; then
    printf 'Installing packaged Rust %s\n' "$version"
    sudo -n env DEBIAN_FRONTEND=noninteractive NEEDRESTART_MODE=l \
        apt-get update "${apt_options[@]}"
    sudo -n env DEBIAN_FRONTEND=noninteractive NEEDRESTART_MODE=l \
        apt-get install -y --no-install-recommends "${apt_options[@]}" \
        "cargo-$version" "rustc-$version"
    cargo=$(command -v "cargo-$version")
    rustc=$(command -v "rustc-$version")
fi
printf 'Checking %s and %s\n' "$cargo" "$rustc"
"$cargo" --version
"$rustc" --version
binary=$(mktemp -d "$RUNNER_TEMP/katzenqt-rust.XXXXXX")
ln -s "$cargo" "$binary/cargo"
ln -s "$rustc" "$binary/rustc"
printf 'CARGO=%s\nRUSTC=%s\n' "$cargo" "$rustc" >> "$GITHUB_ENV"
printf '%s\n' "$binary" >> "$GITHUB_PATH"
