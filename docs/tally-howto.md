# Tally: how to

Recipes for driving the tally protocol — the group-chat survey/voting feature —
through its Python API. Each section is one task with the code that performs
it. For what the types mean and what the protocol guarantees, see the
[API reference](tally-api.md).

Every recipe here is plain `async` Python with no Qt and no assumptions about
the caller: the same functions work from the headless CLI, from a test, or from
a GUI slot.

## Before you start

### The imports

```python
import asyncio
import uuid

from katzenqt import network, persistent
from katzenqt.tally import engine, events, sync
from katzenqt.tally import send as tally_send
from katzenqt.tally.controller import INSTANCE as tally, voter_id_from_read_cap
from katzenqt.tally.schema import Mode, creator_of, slots_of, topic_of, votes_map
```

Use `INSTANCE`. It is the process-wide controller that the network receive path
also uses, so a survey you create is immediately visible to inbound events and
vice versa. Constructing your own `TallyController()` gives you a second,
diverging set of documents.

### The shape of every mutating task

Creating, voting and closing are all the same four steps:

1. mutate the local document through the controller,
2. stage the matching outbound message on the **same** session,
3. `await sess.commit()`,
4. `await network.check_for_new()` to poke the send loop.

The controller never commits — the caller owns the transaction boundary — and
nothing is on its way to the network until step 4. Reading tasks need none of
this: they touch only the local database.

### Which event loop the calls run on

Headless callers have one event loop and can run all four steps on it.

The GUI runs two: Qt's loop for the widgets, and `AsyncioThread`'s loop
(`katzen.py:65`), which owns the thin-client connection. There, follow what
`MainWindow.chat_msg_single_line` (`katzen.py:467`) does — database work
directly on the Qt loop, network calls handed to the other loop:

```python
await self.iothread.run_in_io(network.check_for_new())
```

### Getting the conversation

Every mutating task needs the `Conversation` row, because outbound messages go
on its write capability. By id or by name:

```python
convo = await sess.get(persistent.Conversation, conversation_id)

convo = (await sess.exec(
    persistent.select(persistent.Conversation).where(
        persistent.Conversation.name == "demo")
)).first()
```

## Create a survey

Mint an id, build the document, broadcast its full state.

```python
async def create_survey(conversation_id: int, topic: str,
                        slots: list[str], mode: Mode) -> bytes:
    survey_id = uuid.uuid4().bytes
    async with persistent.asession() as sess:
        convo = await sess.get(persistent.Conversation, conversation_id)
        doc = await tally.create_local(sess, convo, survey_id, topic, mode, slots)
        await tally_send.stage_outbound(
            sess, convo, events.build_create(survey_id, sync.full_state(doc)),
        )
        await sess.commit()
    await network.check_for_new()
    return survey_id
```

`slots` is the list of descriptive texts; slot ids are assigned by position
(`s0`, `s1`, …) and are fixed for the life of the survey. `new_survey_doc`
raises `ValueError` on an empty list.

`mode` is `Mode.APPROVAL` (`yes`/`no`) or `Mode.AVAILABILITY`
(`yes`/`maybe`/`no`), also fixed at creation. `engine.domain(mode)` gives the
permitted availabilities if you need to build controls or validate input.

## List the surveys in a conversation

Offline: no daemon, no network.

```python
async def surveys_in(conversation_id: int):
    async with persistent.asession() as sess:
        return await tally.list_for_conversation(sess, conversation_id)
```

You get the `Doc`s, loading from `TallyState` any the controller does not
already hold in memory. Their metadata comes from the schema accessors:

```python
for doc in docs:
    print(topic_of(doc), engine.tally(doc).status, slots_of(doc))
```

`TallyController.load_all()` pre-loads every stored survey at startup; it is
optional, because the receive path and `list_for_conversation` both load lazily.

## Read the counts and declare the outcome

Both derivations are pure and cheap — call them whenever you need a fresh
answer rather than caching counts.

```python
result = engine.tally(doc)      # TallyResult
out = engine.outcome(result)    # Outcome

for slot in result.slots:
    print(slot.slot_id, slot.text, slot.yes, slot.maybe, slot.no)

if out.kind == "winner":
    print("winner:", out.winners[0].text, out.top_yes)
elif out.kind == "tie":
    print("tie:", [s.text for s in out.winners], out.top_yes)
else:
    print("no winner: nobody voted yes")
```

Two counting rules that surprise people:

- A voter who omitted a slot counts as `no` for that slot, so
  `yes + maybe + no == result.n_voters` on every slot. A slot nobody mentioned
  still shows a full complement of `no`.
- `outcome()` ranks by `yes` alone. In availability mode `maybe` never
  contributes to the winner. If you want `maybe` to count for something,
  compute your own ranking from the `SlotTally` numbers and present it as such.

The result is derivable at any moment, from any number of votes; there is no
point at which counts "become" available.

## Cast a vote

```python
async def cast_vote(conversation_id: int, survey_id: bytes,
                    choice: dict[str, str]) -> int | None:
    async with persistent.asession() as sess:
        convo = await sess.get(persistent.Conversation, conversation_id)
        version = await tally.cast_local_vote(sess, convo, survey_id, choice)
        if version is None:
            return None                     # we do not have that survey
        await tally_send.stage_outbound(
            sess, convo, events.build_vote(survey_id, choice, version),
        )
        await sess.commit()
    await network.check_for_new()
    return version
```

`choice` maps slot id to availability, e.g. `{"s0": "yes", "s1": "no"}`.
Omitted slots count as `no`, so an approval ballot may list only the approved
slots.

Handle two failure modes:

- `cast_local_vote` returns `None` when the survey is unknown locally — most
  often because it has not arrived yet. Wait and retry, or ask for it (see
  [catch-up](#ask-for-a-survey-you-are-missing)).
- It propagates `ValueError` from `engine.apply_vote` for an unknown slot id or
  an availability outside the mode's domain. That is a bug in the caller or bad
  user input, not a network condition.

**Put the returned version on the outbound message.** `cast_local_vote` mints
`current_version + 1` and returns it; that number is what makes the ballot
supersede the voter's earlier one on every peer regardless of arrival order.

## Change a vote

The same call. There is no separate recast path: the new ballot replaces the
old one wholesale, so send the complete choice rather than a delta. The version
the controller mints is one higher each time, and a peer that receives the two
ballots out of order keeps the newer one.

To show a voter their current ballot before they edit it, read it out of the
document (next task).

## Find out who voted, and what for

`engine.tally` aggregates and will not tell you who voted for what. For that,
read the votes map directly and map voter ids back to peers. A voter id is
`blake2b` of that member's BACAP read capability, so every member derives the
same id for the same member:

```python
async def voter_names(sess, conversation) -> dict[str, str]:
    """voter id (hex) -> display name, for members we hold a read cap for."""
    names = {}
    for peer in conversation.peers:          # includes our own peer
        rcw = await sess.get(persistent.ReadCapWAL, peer.read_cap_id)
        if rcw is not None and rcw.read_cap is not None:
            names[voter_id_from_read_cap(rcw.read_cap).hex()] = peer.name
    return names


def ballots(doc) -> dict[str, dict[str, str]]:
    """voter id (hex) -> {slot_id: availability}, version key dropped."""
    votes = votes_map(doc)
    out = {}
    for voter in votes.keys():
        ballot = votes[voter]
        out[voter] = {k: ballot[k] for k in ballot.keys() if k != "_version"}
    return out
```

Joined up:

```python
names = await voter_names(sess, convo)
for voter_hex, ballot in ballots(doc).items():
    print(names.get(voter_hex, voter_hex[:8]), ballot)
```

The local user's own ballot is the entry under their own voter id, which comes
from `conversation.own_peer`:

```python
async def own_voter_id(sess, conversation) -> bytes | None:
    rcw = await sess.get(persistent.ReadCapWAL, conversation.own_peer.read_cap_id)
    return voter_id_from_read_cap(rcw.read_cap) if rcw and rcw.read_cap else None

mine = ballots(doc).get((await own_voter_id(sess, convo)).hex(), {})
```

A voter id with no matching peer is a member whose read capability you do not
hold. Their ballot is counted either way.

## Check who may close a survey

Only the creator may close. `creator_of` returns the creator's voter id, or
`None` for surveys created before the field existed — in which case anyone may.

```python
creator = creator_of(doc)
may_close = creator is None or creator == await own_voter_id(sess, convo)
```

## Close a survey

```python
async def close_survey(conversation_id: int, survey_id: bytes) -> bool:
    async with persistent.asession() as sess:
        convo = await sess.get(persistent.Conversation, conversation_id)
        if not await tally.close_local(sess, convo, survey_id):
            return False        # unknown survey, or we are not the creator
        await tally_send.stage_outbound(
            sess, convo, events.build_close(survey_id),
        )
        await sess.commit()
    await network.check_for_new()
    return True
```

`close_local` performs the creator check itself and logs why it refused, so
`False` covers both "no such survey" and "not yours to close".

Closing is **advisory**: it sets `status` to `closed`, and peers honour that in
what they display, but it does not prevent further votes and a ballot cast
after a close is still counted. Do not present it as a locked ballot box.

## Notice that a survey changed

Inbound tally messages are applied to the document and written to `TallyState`
by the receive path, which signals nothing: tally messages deliberately never
become `ConversationLog` rows, and `network.conversation_update_queue` is only
poked for those. There is no tally notification channel today.

So to follow a survey, re-derive it on a timer and compare:

```python
async def watch(conversation_id: int, survey_id: bytes, on_change, interval=1.0):
    previous = None
    while True:
        async with persistent.asession() as sess:
            row = await sess.get(persistent.TallyState, survey_id)
        if row is not None:
            result = engine.tally(sync.load_doc(row.doc_state))
            if result != previous:          # TallyResult is a frozen dataclass
                previous = result
                await on_change(result)
        await asyncio.sleep(interval)
```

`TallyResult` and `SlotTally` are frozen dataclasses, so `!=` is a correct
value comparison. Reading `TallyState` directly (rather than the controller's
in-memory document) is what the CLI's `tally-vote` and `tally-result` verbs do
while waiting.

## Ask for a survey you are missing

The receive side answers a catch-up request by staging the diff since your
state vector, which is one round trip however much you missed. Nothing in the
codebase sends the request today, but the builder is there:

```python
from pycrdt import Doc

async def request_catchup(conversation_id: int, survey_id: bytes) -> None:
    async with persistent.asession() as sess:
        convo = await sess.get(persistent.Conversation, conversation_id)
        doc = tally.get(survey_id)                  # in-memory documents only
        if doc is None:
            row = await sess.get(persistent.TallyState, survey_id)
            doc = sync.load_doc(row.doc_state) if row is not None else Doc()
        await tally_send.stage_outbound(
            sess, convo,
            events.build_sync_request(survey_id, sync.state_vector(doc)),
        )
        await sess.commit()
    await network.check_for_new()
```

When you hold nothing at all, send the state vector of an **empty document**
(`sync.state_vector(Doc())`, which is `b"\x00"`) — never `b""`. A peer
answering the request calls `sync.diff_since`, and pycrdt raises
`ValueError: Cannot decode state` on an empty or malformed vector. Against a
valid empty vector it returns the whole document, which is exactly the
catch-up you wanted.

You must know the `survey_id` to ask about it — there is no "what surveys
exist?" request in the protocol. The other way to bring a peer up to date is
for anyone holding the survey to re-broadcast `events.build_create` with
`sync.full_state(doc)`; a receiver that already has the document merges the
update rather than duplicating it.

## Receive inbound tally messages

Nothing to wire: `katzenqt.conversation_handlers.dispatch` already routes the
five tally message kinds to `tally.handle_event`, which applies them and
persists the result. Votes land under the authenticated sender's voter id, so
a peer cannot cast or overwrite anyone else's ballot.

Call `handle_event` yourself only if you are driving the protocol outside the
normal receive path, e.g. in a test:

```python
signal_send = await tally.handle_event(sess, peer, gcm)
if signal_send:                 # it staged a sync response
    await sess.commit()
    await network.check_for_new()
```

`peer` is the `ConversationPeer` the message arrived from — that is the whole
of the authentication, so never pass a peer the message did not come from.

## Drive it from the command line

The five verbs cover the same tasks; `tally-list` needs no daemon.

```shell
KH=.venv/bin/katzenqt-headless
CONN="--address 127.0.0.1:64331"

S=$(KQT_STATE=/tmp/alice $KH tally-create demo "Where to eat?" \
      --mode approval --slot Pizza --slot Sushi --slot Tacos $CONN 2>&1 \
      | sed -n 's/^TALLY_CREATED=//p')
KQT_STATE=/tmp/bob   $KH tally-vote demo --survey $S --slot s0=yes --slot s2=yes $CONN
KQT_STATE=/tmp/alice $KH tally-result demo --survey $S --expect-voters 2 $CONN
KQT_STATE=/tmp/alice $KH tally-close demo --survey $S $CONN
KQT_STATE=/tmp/alice $KH tally-list demo
```

Output goes to stderr, one result token per verb; match by substring. The full
argument list and the exit codes are in the
[API reference](tally-api.md#the-headless-cli). This is also the quickest way
to produce a state file with real surveys in it to develop against.

## Exercise the protocol with no daemon and no network

The core is verifiable in full with two in-process documents — this is how
`tests/test_tally_sync.py` and `tests/test_tally_convergence.py` work.

```python
from katzenqt.tally import engine, schema, sync
from katzenqt.tally.schema import Mode

alice = schema.new_survey_doc(b"sid", "lunch?", Mode.APPROVAL, ["Pizza", "Sushi"])
engine.apply_vote(alice, b"alice-id", {"s0": "yes"})

bob = sync.load_doc(sync.full_state(alice))     # Bob receives the broadcast
engine.apply_vote(bob, b"bob-id", {"s1": "yes"})

# One exchange in each direction and the two agree.
sync.apply_update(alice, sync.diff_since(bob, sync.state_vector(alice)))
sync.apply_update(bob, sync.diff_since(alice, sync.state_vector(bob)))
assert engine.tally(alice) == engine.tally(bob)
```

For the controller and the database, `tests/test_tally_controller.py` shows the
setup: an in-memory conversation with provisioned read capabilities, and no
transport at all.

## Pitfalls

- **Wait for the read capability.** A voter's identity is the hash of their
  provisioned BACAP read cap. Before it exists the controller logs a warning
  and falls back to a *local-only* id no peer will derive, so such a ballot
  never merges with that voter's others. Do not vote on a conversation that is
  not fully joined.
- **Send on the write capability.** `stage_outbound` handles it, but a
  hand-built `SendOperation` must use `conversation.write_cap`; any other
  stream is silently never sent.
- **Commit before poking.** `check_for_new()` ahead of the commit races the
  send loop against your own transaction.
- **`survey_id` is bytes** throughout the API and hex wherever a human sees it.
  Convert once, at the boundary.
- **Slots and mode are immutable.** There is no edit-the-question, no
  add-a-slot, no remove-a-slot.
- **Staged is not sent.** All four mutating tasks return once the message is
  staged; delivery crosses the mixnet, which takes seconds on the docker
  mixnet and can take minutes on a real one. To track a specific message, keep
  the `PlaintextWAL` id `stage_outbound` returns and watch for it in
  `SentLog` — that is what the CLI's `SENT`/`VOTED`/`CLOSED` tokens mean.
- **A state vector is never `b""`.** The empty-document vector is `b"\x00"`.
  An empty or corrupt vector makes the responding peer's `diff_since` raise
  `ValueError`, and `handle_event` does not catch it.
- **Large surveys are chunked.** Anything over 1530 bytes (a big
  `TALLY_CREATE`, a long `TALLY_SYNC_RESP`) is split across a BACAP sub-stream
  and takes correspondingly longer to arrive.
