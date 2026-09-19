set -euo pipefail

toolchain=1.98.1
cargo_home=${CARGO_HOME:-$HOME/.cargo}
export PATH="$cargo_home/bin:$PATH"
if ! command -v rustup >/dev/null 2>&1; then
    sudo apt-get update
    sudo apt-get install -y --no-install-recommends rustup
fi
rustup toolchain install "$toolchain" --profile minimal
rustup run "$toolchain" rustc --version
rustup run "$toolchain" cargo --version
cargo=$(rustup which --toolchain "$toolchain" cargo)
printf 'RUSTUP_TOOLCHAIN=%s\n' "$toolchain" >> "${GITHUB_ENV:?}"
printf '%s\n' "$(dirname "$cargo")" >> "${GITHUB_PATH:?}"
