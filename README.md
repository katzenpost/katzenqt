# katzenqt

`katzenqt` is a PySide6 GUI chat client for Katzenpost mixnet group chat. It can
also be driven headlessly from the command line.

## Project status

This is pre-alpha developer software. There is **no easily installable package
yet**, so the instructions below are aimed at developers building and running
from source against a Katzenpost client daemon (`kpclientd`). A single,
well-documented install command for end users is future work. Please read the
warning at the end of this document.

## Running the GUI

On Debian GNU/Linux, install `git` and `make`, clone the repo, and build:

```shell
sudo apt install -y git make
git clone https://www.github.com/katzenpost/katzenqt ~/katzenqt
cd ~/katzenqt
make deps      # full bootstrap: system packages, the venv, kpclientd, a systemd unit
make run       # launch the GUI
```

`make deps` is a first-time bootstrap and also runs the test suite. If you only
need to refresh Python dependencies, use `uv sync` instead. If `make run` does
not bring up a window, run the steps individually:

```shell
make system-setup
make setup-uv
make setup
make kpclientd
make install-kpclient
make kpclientd.service
make status
```

`make status` reports a working setup roughly as:

```
backend: uv
venv: .venv
kpclientd(bin): /home/user/.local/bin/kpclientd
kpclientd(service): active
kpclientd(path): found
```

Once that looks right, `make run` launches the GUI.

## Headless CLI (no GUI)

`katzenqt` can be driven without the Qt interface through the `katzenqt-headless`
command installed in the venv. It talks to a running `kpclientd` and is quiet by
default, printing only each verb's result token (set `KQT_LOG_LEVEL=DEBUG` for
the full log).

```shell
.venv/bin/katzenqt-headless --help
```

Two things to know:

- **The connection is explicit.** Every verb that talks to the daemon needs one
  of `--config <thinclient.toml>` or `--address <addr> [--network tcp|unix]`.
- **Each identity is a separate `KQT_STATE`** (its own SQLite file), so two
  parties on one machine are just two `KQT_STATE` values sharing one daemon.

The walkthrough below takes Alice and Bob through **joining a conversation with
the Contact Voucher protocol**, then **exchanging messages**, then **running a
vote**. It assumes a daemon reachable on TCP `127.0.0.1:64331` (for example the
docker mixnet under `katzenpost/docker`); change `$CONN` to match yours.

```shell
KH=.venv/bin/katzenqt-headless
CONN="--address 127.0.0.1:64331"     # or: --config /path/to/thinclient.toml
ALICE=/tmp/alice; BOB=/tmp/bob

# 1. Join the same conversation using the voucher token protocol
KQT_STATE=$ALICE $KH create-conv demo alice $CONN
KQT_STATE=$BOB   $KH create-conv demo bob   $CONN
V=$(KQT_STATE=$BOB $KH voucher-mint demo bob $CONN 2>&1 | sed -n 's/^VOUCHER=//p')   # Bob mints a token
KQT_STATE=$ALICE $KH voucher-induct demo bob "$V" $CONN                              # Alice inducts Bob
KQT_STATE=$BOB   $KH voucher-await demo $CONN                                        # -> JOINED

# 2. Exchange messages (the channel is bidirectional)
KQT_STATE=$ALICE $KH send demo "hi bob"   $CONN
KQT_STATE=$BOB   $KH read demo 600 $CONN          # -> RECV=hi bob
KQT_STATE=$BOB   $KH send demo "hi alice" $CONN
KQT_STATE=$ALICE $KH read demo 600 $CONN          # -> RECV=hi alice

# 3. Vote: Bob opens an approval poll, both vote, the tool declares the winner
S=$(KQT_STATE=$BOB $KH tally-create demo "Where to eat?" \
      --mode approval --slot Pizza --slot Sushi --slot Tacos $CONN 2>&1 | sed -n 's/^TALLY_CREATED=//p')
KQT_STATE=$BOB   $KH tally-vote demo --survey $S --slot s0=yes --slot s1=yes $CONN   # Bob: Pizza, Sushi
KQT_STATE=$ALICE $KH tally-vote demo --survey $S --slot s0=yes --slot s2=yes $CONN   # Alice: Pizza, Tacos
KQT_STATE=$BOB   $KH tally-close demo --survey $S $CONN                              # only the creator may close
KQT_STATE=$ALICE $KH tally-result demo --survey $S --expect-voters 2 $CONN
# -> TALLY={..., "outcome": "winner", "winners": [{"slot_id": "s0", "text": "Pizza", "yes": 2}]}
# -> WINNER=Pizza (2 yes)
```

Every network step crosses the mixnet, so over a real network each can take from
seconds to minutes (the docker mixnet is near-instant). The full set of verbs,
`info`, `multi-send`, `send-file`, `read-file`, `chat-session`, `tally-list`, and
so on, is listed by `--help`; see HACKING.md for the fuller reference.

## Warning

DO NOT USE THIS SOFTWARE UNLESS YOU ARE DEVELOPING IT AND AWARE OF THE TECHNICAL
RISKS. DO NOT RELY ON THIS SOFTWARE FOR ANONYMITY, SECURITY, PRIVACY,
RELIABILITY, RESILENCY, CREDIBILITY, FRIENDSHIP, FUN, OR ANY SERIOUS BUSINESS.
