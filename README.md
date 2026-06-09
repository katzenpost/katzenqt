So you want to try the `katzenqt` program? These instructions should help
developers to start to run the software.

WARNING: DO NOT USE THIS SOFTWARE UNLESS YOU ARE DEVELOPING IT AND AWARE OF THE
TECHNICAL RISKS. DO NOT RELY ON THIS SOFTWARE FOR ANONYMITY, SECURITY, PRIVACY,
RELIABILITY, RESILENCY, CREDIBILITY, FRIENDSHIP, FUN, OR ANY SERIOUS BUSINESS.

To use the new `Makefile` targets please ensure that you have `git` and `make`
installed before proceeding:
```
  sudo apt install -y git make
```

First, git clone the `katzenqt` repo and `cd` into `katzenqt`:
```
  git clone https://www.github.com/katzenpost/katzenqt ~/katzenqt
  cd ~/katzenqt/
```

At this point your system should have all of the required tools to proceed.
Your user account should have `~/.local/katzenpost/client.toml` and
`~/.local/katzenpost/thinclient.toml` ready for software that expects to find
the configuration files in those locations.

Run these two commands on Debian GNU/Linux to setup the system, the environment
and to run `katzenqt`:
```
make deps
make run
```

Or you could run a series of make targets inside `katzenqt` if the previous
commands did not result in `katzenqt` displaying a user interface:
```
  make system-setup
  make setup-uv
  make setup
  make test
  make kpclientd
  make install-kpclient
  make kpclientd.service
  make status
```

If the `make status` shows the following then things are probably working:
```
backend: uv
venv: .venv
kpclientd(bin): /home/user/.local/bin/kpclientd
kpclientd(service): active
kpclientd(path): found
```

It should then be possible to run `katzenqt` using your configured and prepared
virtual environment as shown by the `make status` command. There are several
ways to run `katzenqt` and one that is expected to work at this point is `make
run`:
```
  make run
```

## Headless mode (no GUI)

`katzenqt` can also be driven without the Qt interface, through the
`katzenqt.headless` command-line tool. It talks to a running `kpclientd` over
the thin-client protocol and is useful for scripting and testing. Invoke it
through the project's virtualenv (the bare `katzenqt-headless` name is only on
`PATH` once the venv is activated):

```
.venv/bin/katzenqt-headless --help
# or, as a module:
.venv/bin/python -m katzenqt.headless --help
```

It is one command with many subcommands: `create-conv`, the Contact Voucher
handshake (`voucher-mint`, `voucher-induct`, `voucher-await`), messaging
(`send`, `read`, `send-file`, `read-file`, ...), an offline state summary
(`info`), and the tally/voting protocol (`tally-create`, `tally-vote`,
`tally-result`). Append `--help` to any subcommand for its arguments.

**The connection is explicit; nothing is searched for on disk.** Every verb that
talks to the daemon requires exactly one of:

- `--config <thinclient.toml>` , a thin-client config file, or
- `--address <addr>` with an optional `--network {tcp,unix}` (default `tcp`),
  for example `--address 127.0.0.1:64331` (a docker mixnet) or
  `--address @katzenpost --network unix` (a unix socket).

`info` is the one verb that needs no daemon and so takes no connection argument.
Each identity is a separate `KQT_STATE` (see HACKING.md); run separate
identities as separate processes. For example, a survey created and read over a
local docker mixnet:

```
KQT_STATE=alice .venv/bin/katzenqt-headless tally-create demo "lunch?" \
    --mode approval --slot A --slot B --slot C \
    --address 127.0.0.1:64331            # prints TALLY_CREATED=<hex>
KQT_STATE=alice .venv/bin/katzenqt-headless tally-result demo --survey <hex> \
    --address 127.0.0.1:64331            # prints TALLY={...}
```

See HACKING.md for the fuller headless walkthrough.

These instructions are temporary and later the user story should include a
single command that will be well documented.
