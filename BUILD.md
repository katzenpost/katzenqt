# KatzenQT — Build

Instructions for compiling dependencies, installing the Python environment, and
preparing `kpclientd`. Do this once on a new machine (or after `make clean`).

> **Warning:** This software is experimental. Do not rely on it for anonymity,
> security, or production use.

## Prerequisites

On Debian/Ubuntu:

```bash
sudo apt install -y git make
```

The `Makefile` installs additional system packages (Qt libraries, Go toolchain
helpers, `pipx`, `podman`, etc.) via `make system-setup`.

You also need Katzenpost configuration files before the client can connect to a
mixnet. Place them in `~/.local/katzenpost/`:

```bash
mkdir -p ~/.local/katzenpost/
cp config/client.toml ~/.local/katzenpost/client.toml
cp config/thinclient.toml ~/.local/katzenpost/thinclient.toml
```

For a real network (for example [namenlos](https://github.com/katzenpost/namenlos)),
copy that project's `client.toml` and `thinclient.toml` instead of the bundled
defaults.

## Clone

```bash
git clone https://github.com/katzenpost/katzenqt ~/katzenqt
cd ~/katzenqt
```

## Quick build (recommended)

From the `katzenqt` directory, one target installs system packages, the Python
venv, runs tests, builds `kpclientd`, installs it, and enables the user systemd
service:

```bash
make deps
```

When finished, verify:

```bash
make status
```

Expected output:

```text
backend: uv
venv: .venv
kpclientd(bin): /home/<user>/.local/bin/kpclientd
kpclientd(service): active
kpclientd(path): found
```

## Step-by-step build

Use these targets if you prefer explicit control or if `make deps` fails partway
through.

### 1. System packages and `uv`

```bash
make system-setup    # apt packages + pipx install uv
make setup-uv       # create .venv and install katzenqt (uv backend)
make setup          # confirm backend is configured
```

Alternative Python backend (standard `venv` + `pip`):

```bash
make setup-pip
```

### 2. Run tests (optional but recommended)

```bash
make test
```

### 3. Build and install `kpclientd`

`kpclientd` is the Katzenpost thin-client daemon the GUI talks to over a local
socket.

```bash
make katzenpost-update   # clone/pull katzenpost into ./katzenpost
make kpclientd           # native Go build; falls back to podman if Go fails
make install-kpclient    # install binary + default configs to ~/.local/
make kpclientd.service   # install and enable user systemd unit
```

### 4. Push-to-talk audio (optional)

Push-to-talk needs the Rust PyO3 extension from the sibling
` Rustic_Audio_PyO3` crate. Without it, the client runs but PTT stays disabled.

```bash
# from katzenqt/, after make setup-uv
make rust-audio
```

`make rust-audio` builds the Rustic crate (Cargo emits `librustic_audio_tool.so`
on Linux) and installs `src/katzenqt/audio/rustic_audio_tool.so`.

Rust prerequisites: `build-essential`, `pkg-config`, `libasound2-dev`, and a
current toolchain (`rustup`). See `../ Rustic_Audio_PyO3/BUILD.md` for details.

### 5. Qt UI code generation

`make run` regenerates Qt resources when `.ui` or `.qrc` files change. To build
them explicitly:

```bash
make code-generator    # only if inputs changed
make regen-code        # force full regeneration
```

## Maintenance

| Target | Purpose |
|--------|---------|
| `make clean-venv` | Remove `.venv`; rerun `make setup-uv` or `make setup-pip` |
| `make clean` | Remove `.venv`, system stamp, and generated Qt files |
| `make katzenpost-update` | Update the `katzenpost` submodule checkout |

## Next step

When `make status` looks healthy, see [RUN.md](RUN.md) for launching the client.
