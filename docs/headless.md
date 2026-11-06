# Headless CLI

[Back to the README](../README.md)

katzenqt can be driven without its Qt interface. The `katzenqt.headless` module talks to a
running `kpclientd` over the thin client protocol and imports nothing from PySide6. It
offers a small async Python API (`connect`, `start`, `stop`, and the `session` context
manager) and a line-oriented CLI, installed in the venv as `katzenqt-headless`. Its
shebang points at the venv's Python, so no activation is needed:

```shell
.venv/bin/katzenqt-headless --help
```

## Verbs

- Conversation setup: `create-conv`.
- The Contact Voucher handshake: `voucher-mint`, `voucher-induct`, `voucher-await`.
- Messaging: `send`, `multi-send`, `read`, `chat-session`, `send-file`, `read-file`.
- A summary of the state file: `info`.
- Polls: `tally-create`, `tally-vote`, `tally-close`, `tally-result`, `tally-list`.

Append `--help` to any verb for its arguments. The tally verbs and the protocol behind
them are documented in the [Tally API](tally-api.md), with a task-by-task guide in the
[Tally how-to](tally-howto.md).

## Things to know

- **The connection is explicit; there is no filesystem search.** Every verb that talks to
  the daemon needs exactly one of `--config <thinclient.toml>` or `--address <addr>`, with
  an optional `--network {tcp,unix}` (default `tcp`). Use `--address 127.0.0.1:64331` for
  the docker mixnet, or `--address @katzenpost --network unix` for a daemon on the
  abstract Unix socket. `info` and `tally-list` are offline and take no connection
  argument. The Python API takes the config path as an explicit argument too.
- **Each identity is a separate `KQT_STATE`.** The state file is chosen once, at import,
  from `KQT_STATE` (see [Running](running.md#state)), so run separate identities as
  separate processes. Two parties on one machine are two `KQT_STATE` values sharing one
  daemon.
- **One connection per process.** A process may run only one connect and shutdown
  cycle, so a verb that must receive and then send (such as `tally-vote`, which awaits the
  survey before casting) does all of it on one connection.
- **Quiet by default.** The CLI prints only each verb's result token (`CREATED`,
  `VOUCHER=`, `JOINED`, `SENT`, `RECV=`, `TALLY_CREATED=`, `VOTED`, `CLOSED`,
  `TALLY=<json>`, `WINNER=` or `TIE=`), with any warnings and errors, all on stderr. A
  harness should match a token by substring. Set `KQT_LOG_LEVEL` to `INFO` or `DEBUG` for
  the full log.

## Walkthrough

Alice and Bob join a conversation with the Contact Voucher protocol, exchange messages,
and run a vote. This assumes a daemon on TCP `127.0.0.1:64331`, such as the docker mixnet
from [Testing](testing.md); change `$CONN` to match yours.

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
KQT_STATE=$BOB   $KH tally-close demo --survey $S $CONN                              # advisory; only the creator may close
KQT_STATE=$ALICE $KH tally-result demo --survey $S --expect-voters 2 $CONN
# -> TALLY={..., "outcome": "winner", "winners": [{"slot_id": "s0", "text": "Pizza", "yes": 2}]}
# -> WINNER=Pizza (2 yes)
```

The winner can be derived from the votes at any time, so `tally-close` is not needed to
read the result. Closing is advisory: it sets the survey's status to `closed`, and only
the creator may do so, but it does not yet prevent further votes; a ballot cast after a
close is still counted. Enforcing "no votes after close" convergently across peers needs
causal ordering and is future work.

Every network step crosses the mixnet, so over a real network each can take from seconds
to minutes; on the docker mixnet they are near-instant.
