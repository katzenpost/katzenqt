# katzenqt

[![pytest](https://github.com/katzenpost/katzenqt/actions/workflows/pytest.yml/badge.svg?branch=main)](https://github.com/katzenpost/katzenqt/actions/workflows/pytest.yml)

katzenqt is a desktop group chat client for the [Katzenpost](https://katzenpost.network)
mixnet. It reaches the mixnet through the client daemon
[kpclientd](https://github.com/katzenpost/katzenpost/tree/main/cmd/kpclientd), and it can
also be driven from the command line without its Qt interface.

![katzenqt in dark mode, showing a group conversation](docs/screenshot.png)

## Quick start

There are no release packages yet. On Debian or Ubuntu, build from source:

```shell
sudo apt install -y git make
git clone https://github.com/katzenpost/katzenqt ~/katzenqt
cd ~/katzenqt
make deps
make run
```

If `sudo` refuses you, run `su -c 'apt install -y git make'` instead.

katzenqt needs kpclientd running. `make deps` installs the system packages and the
Python venv, runs the tests, builds kpclientd from a pinned
[katzenpost](https://github.com/katzenpost/katzenpost) checkout, and starts it as a user
service that connects to the namenlos mixnet. `make status` shows whether it is active;
[Building](docs/building.md) takes the steps one at a time.

## Docs

- [Building](docs/building.md): the setup steps, Python backends, kpclientd
- [Running](docs/running.md): configuration, state files, the daemon, troubleshooting
- [Headless CLI](docs/headless.md): joining, messaging and voting from a shell
- [Testing](docs/testing.md): unit tests, the docker mixnet, two GUIs on one machine
- [Packaging](docs/packaging.md): Debian packages and the Flatpak
- [Hacking](docs/hacking.md): code layout, database migrations, UI editing
- [Tally API](docs/tally-api.md) and [Tally how-to](docs/tally-howto.md): polls and votes
- [Group chat design](https://katzenpost.network/docs/specs/groupchat/) and
  [thin client spec](https://katzenpost.network/docs/specs/thinclient/), on the website

katzenqt is part of the [Katzenpost](https://github.com/katzenpost) organization.

## Warning

DO NOT USE THIS SOFTWARE UNLESS YOU ARE DEVELOPING IT AND AWARE OF THE TECHNICAL
RISKS. DO NOT RELY ON THIS SOFTWARE FOR ANONYMITY, SECURITY, PRIVACY,
RELIABILITY, RESILENCY, CREDIBILITY, FRIENDSHIP, FUN, OR ANY SERIOUS BUSINESS.

## License

Public domain, as declared in [pyproject.toml](pyproject.toml), except
`src/katzenqt/theme.py`, which is AGPL-3.0-only; see [debian/copyright](debian/copyright).
