set -euo pipefail

sudo apt-get update
sudo apt-get install -y --no-install-recommends \
    build-essential pkg-config libasound2-dev libxcb-cursor0 libegl1 \
    libpulse0 libfontconfig1 libxkbcommon0
uv sync --all-extras --dev --locked --python 3.12
.venv/bin/python -c "import rustic_audio_tool as r; assert hasattr(r, 'PttAudioEngine')"
