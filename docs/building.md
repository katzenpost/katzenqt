# Building katzenqt

[Back to the README](../README.md)

These steps prepare a Debian or Ubuntu machine to run katzenqt from source: system
packages, the Python environment, and the client daemon `kpclientd`. Do this once on a
new machine, or after `make clean`. When it is done, see [Running](running.md).

## Prerequisites

```shell
sudo apt install -y git make
git clone https://github.com/katzenpost/katzenqt ~/katzenqt
cd ~/katzenqt
```

Debian may not add the first user to `sudoers`. If `sudo` refuses you, run the `apt` step
as root instead (`su -c 'apt install -y git make'`). The Makefile's own `apt` steps do the
same: when `sudo` is unavailable they stop and print the `su -c` line to paste.

## One command

```shell
make deps
```

`make deps` installs the system packages (Qt libraries, `podman`, `pipx`, and `cargo`
with `libasound2-dev` for the push-to-talk audio extension), installs `uv`, creates the
venv in `.venv`, runs the test suite, builds `kpclientd`, installs it, and enables it as
a user systemd service. Then check the result:

```shell
make status
```

A working setup looks like this:

```text
backend: uv
venv: .venv
kpclientd(bin): /home/<user>/.local/bin/kpclientd
kpclientd(service): active
kpclientd(path): found
```

If you only need to refresh the Python dependencies, for example after a new dependency
lands in `pyproject.toml`, run `uv sync` (or `make setup-uv`) instead; neither runs the
tests.

## Step by step

Use these targets for explicit control, or when `make deps` fails partway.

```shell
make system-setup        # apt packages, then uv through pipx
make setup-uv            # create .venv and install katzenqt with uv
make setup               # confirm a backend is configured
make test                # run the test suite
make kpclientd           # build kpclientd
make install-kpclient    # install it and the namenlos configs
make kpclientd.service   # install and enable the user systemd unit
make status
```

To use the standard `venv` and `pip` instead of `uv`, run `make setup-pip` in place of
`make setup-uv`. `make run`, `make test` and `make mypy` use whichever backend is set up.

## kpclientd

`kpclientd` is the Katzenpost client daemon; katzenqt talks to it over a local socket.
`make kpclientd` clones [katzenpost](https://github.com/katzenpost/katzenpost) into
`./katzenpost` at the revision pinned by `KATZENPOST_REV` in the Makefile and builds
`cmd/kpclientd` with Go. If the Go build fails it falls back to `make kpclientd-podman`,
which builds the daemon in the katzenpost container toolchain. `make katzenpost-update`
moves the checkout to the pinned revision after the pin changes.

`make install-kpclient` installs the binary to `~/.local/bin/kpclientd`, and writes the
namenlos configs from `src/katzenqt/data/` to `~/.local/katzenpost/client.toml` and
`~/.local/katzenpost/thinclient.toml`. To use another network, replace `client.toml` with
that network's config.

## Push-to-talk audio

Push-to-talk uses the `rustic_audio_tool` extension from
[Rustic_Audio_PyO3](https://github.com/katzenpost/Rustic_Audio_PyO3). It is an ordinary
dependency in `pyproject.toml`, so `uv` builds it with Cargo when it creates the venv; it
needs `cargo` and `libasound2-dev`, which `make deps` installs. Without the extension the
client runs, but push-to-talk stays disabled.

## Qt code generation

`make run` regenerates the Qt resource and UI modules when `ui/*.ui` or
`resources/resources.qrc` change. To do it by hand:

```shell
make code-generator    # only if the inputs changed
make regen-code        # force a full regeneration
```

## Maintenance

| Target | Purpose |
|--------|---------|
| `make clean-venv` | Remove `.venv`; rerun `make setup-uv` or `make setup-pip` |
| `make clean` | Remove `.venv` and the system-setup stamp |
| `make katzenpost-update` | Move `./katzenpost` to the pinned revision |
| `make help` | List every target |
