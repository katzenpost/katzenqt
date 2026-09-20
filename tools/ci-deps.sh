set -euo pipefail

apt_options=(
    -o Acquire::Retries=1
    -o Acquire::http::Timeout=30
    -o Acquire::https::Timeout=30
    -o DPkg::Lock::Timeout=30
    -o Dpkg::Use-Pty=0
)
sudo -n env DEBIAN_FRONTEND=noninteractive NEEDRESTART_MODE=l \
    apt-get update "${apt_options[@]}"
sudo -n env DEBIAN_FRONTEND=noninteractive NEEDRESTART_MODE=l \
    apt-get install -y --no-install-recommends "${apt_options[@]}" \
    build-essential pkg-config libasound2-dev libxcb-cursor0 libegl1 libgl1 \
    libpulse0 libfontconfig1 libxkbcommon0
uv sync --all-extras --dev --locked --python 3.12
.venv/bin/python -c "import rustic_audio_tool as r; assert hasattr(r, 'PttAudioEngine')"
.venv/bin/python -c "from PySide6.QtGui import QGuiApplication"
