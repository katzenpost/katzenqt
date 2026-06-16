# KatzenQT — Run

Instructions for starting the client after [BUILD.md](BUILD.md) is complete.

## Daily launch

From the `katzenqt` directory:

```bash
make status              # confirm backend + kpclientd state
make kpclientd.service   # safe to rerun; ensures daemon is enabled and running
make run                 # launch the GUI
```

`make run` selects the configured Python backend (`uv` or `pip`), regenerates Qt
UI code if needed, and starts `katzenqt`.

## Alternative launch commands

With the **uv** backend (default after `make setup-uv`):

```bash
uv run katzenqt
```

With the **pip** backend:

```bash
.venv/bin/katzenqt
```

## Headless mode

For scripts, tests, or automation without the Qt GUI:

```bash
uv run katzenqt-headless --help
# or
uv run python -m katzenqt.headless --help
```

## Configuration

| File | Typical location |
|------|------------------|
| Katzenpost client config | `~/.local/katzenpost/client.toml` |
| Thin-client transport config | `~/.config/katzenqt/thinclient.toml`, or bundled `katzenqt/data/thinclient.toml` |

`kpclientd` reads `~/.local/katzenpost/client.toml`. The GUI resolves
`thinclient.toml` via several fallbacks (see `network.py`).

## Data storage

Persistent state (keys, messages, contacts) is stored in:

```text
~/.local/share/katzenqt/katzen.sqlite3
```

Use a separate database for testing:

```bash
KQT_STATE=testdb make run
# uses ~/.local/share/katzenqt/testdb.sqlite3
```

## Backend daemon

`kpclientd` must be running before the client connects.

```bash
systemctl --user status kpclientd
journalctl --user -u kpclientd -f
systemctl --user restart kpclientd
systemctl --user stop kpclientd
```

The service listens on the abstract Unix socket `@katzenpost`. Only one
`kpclientd` instance (user or system) should own that socket.

To keep the user service running after logout:

```bash
loginctl enable-linger "$USER"
```

## Troubleshooting

**Connection refused (`127.0.0.1:64331` or socket errors)**

- Check `systemctl --user status kpclientd`
- Confirm configs exist under `~/.local/katzenpost/`
- Restart: `systemctl --user restart kpclientd`

**GUI fails to open (missing Qt libraries)**

- Rerun `make system-setup` or install `libxcb-cursor0`, `libegl1`,
  `libpulse0`, `libfontconfig1`, `libxkbcommon0`

**Push-to-talk disabled**

- Build the Rust audio extension (see [BUILD.md](BUILD.md)) and confirm
  `src/katzenqt/audio/rustic_audio_tool.so` exists

**Stale virtual environment**

```bash
make clean-venv
make setup-uv
```
