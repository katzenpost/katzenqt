# Running katzenqt

[Back to the README](../README.md)

This page assumes [Building](building.md) is done and `make status` looks healthy.

## Launch

From the `katzenqt` directory:

```shell
make status              # backend and kpclientd state
make kpclientd.service   # safe to rerun; makes sure the daemon is enabled and running
make run                 # launch the GUI
```

`make run` picks the configured Python backend, regenerates the Qt code if needed, and
starts `katzenqt`. With the uv backend, `uv run katzenqt` does the same; with the pip
backend, `.venv/bin/katzenqt`. To drive katzenqt without the GUI, see
[Headless CLI](headless.md).

## Configuration

| File | Read by | Location |
|------|---------|----------|
| Katzenpost client config | `kpclientd` | `~/.local/katzenpost/client.toml` |
| Thin client config | katzenqt | the first that exists of `$KATZENQT_THINCLIENT_CONFIG`, `~/.config/katzenqt/thinclient.toml` (under `$XDG_CONFIG_HOME`), and the copy bundled in `katzenqt/data/thinclient.toml` |

The bundled thin client config dials the daemon's abstract Unix socket `@katzenpost`.
`config/thinclient.docker.toml` dials the docker mixnet's daemon on TCP
`127.0.0.1:64331` instead; see [Testing](testing.md).

## State

Keys, messages and contacts are stored in one SQLite file:

```text
~/.local/share/katzenqt/katzen.sqlite3
```

The directory follows `$XDG_DATA_HOME`. `KQT_STATE` replaces the file name, which gives a
separate identity, for testing or for talking to yourself:

```shell
KQT_STATE=testdb make run    # uses ~/.local/share/katzenqt/testdb.sqlite3
```

Only one katzenqt process may use a state file at a time.

## The daemon

`kpclientd` must be running before katzenqt connects. It runs as a user service:

```shell
systemctl --user status kpclientd
journalctl --user -u kpclientd -f
systemctl --user restart kpclientd
systemctl --user stop kpclientd
```

The service listens on the abstract Unix socket `@katzenpost` and on
`$XDG_RUNTIME_DIR/katzenpost/kpclientd.sock`. Only one `kpclientd` instance, user or
system, should own those sockets. To keep the user service running after you log out:

```shell
loginctl enable-linger "$USER"
```

## Troubleshooting

**Connection refused, or socket errors**

- Check `systemctl --user status kpclientd`.
- Confirm the configs exist under `~/.local/katzenpost/`.
- Restart it: `systemctl --user restart kpclientd`.

**The GUI does not open (missing Qt libraries)**

- Rerun `make system-setup`, or install `libxcb-cursor0`, `libegl1`, `libpulse0`,
  `libfontconfig1` and `libxkbcommon0`.

**`KatzenQt is already running.`**

- Another process holds the state file. Close it, or pick another `KQT_STATE`.

**Push-to-talk is disabled**

- The `rustic_audio_tool` extension did not build or import; see
  [Push-to-talk audio](building.md#push-to-talk-audio).

**A stale virtual environment**

```shell
make clean-venv
make setup-uv
```
