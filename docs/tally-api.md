# The tally API

`katzenqt.tally` implements **tally**: a decentralised survey ("vote", "poll",
"Doodle") that runs over an existing group conversation. Every member holds a
replica of the survey; the replicas converge without a coordinator because the
survey's state is a [pycrdt](https://github.com/y-crdt/pycrdt) `Doc`, and the
counts are never transmitted — each peer derives them from the votes it holds.

This document is the reference. For task recipes — create a survey, cast a
vote, read the result, catch up after missing messages — see
[tally-howto.md](tally-howto.md).

## Status

The protocol core, the persistence, the receive-side routing and the headless
CLI verbs exist and are covered by unit, property and docker-integration tests.
There is **no GUI yet**, and two pieces of the design are deliberately still
open; both are called out under [What is not enforced](#what-is-not-enforced)
and [Known gaps](#known-gaps). Nothing in the API below is frozen, but all of
it is in use by the CLI and the integration tests, so changes should be made
deliberately rather than by accident.

## Concepts

| Term | Meaning |
|---|---|
| **survey** | One question with a fixed set of slots. Identified by a `survey_id` (opaque bytes; the CLI mints `uuid.uuid4().bytes`). One survey is one CRDT `Doc` and one `TallyState` row. |
| **slot** | One option to vote on. Slots are fixed at creation and identified positionally: `s0`, `s1`, … (`schema.slot_id(i)`). Each carries a descriptive `text`. |
| **mode** | `approval` (availability domain `yes`/`no`) or `availability` (`yes`/`maybe`/`no`, Doodle-style). Fixed at creation. |
| **availability** | One voter's answer *for one slot*: a string from the mode's domain. |
| **choice** | One voter's whole ballot: a `dict[slot_id, availability]`. Omitted slots count as `no`. |
| **voter id** | 16 bytes, `blake2b(peer's BACAP read capability, digest_size=16)`. Peer-independent: every member derives the same id for the same member, because they all hold the same read cap for them. |
| **version** | A per-voter monotonic counter carrying intent order, so a recast supersedes an earlier ballot whatever the arrival order. |
| **status** | `open` or `closed`. Closing is advisory (see below). |
| **outcome** | The declared result: `winner`, `tie`, or `no_winner`, derived purely from yes counts. |

## Module map

The core is Qt-free and network-free, and must stay so.

| Module | Depends on | Role |
|---|---|---|
| `katzenqt.tally.schema` | pycrdt | The data model: how a survey lives inside a `Doc`. |
| `katzenqt.tally.engine` | schema | Local mutations (`apply_vote`, `close_survey`) and the pure derivations (`tally`, `outcome`). |
| `katzenqt.tally.sync` | pycrdt | State vectors, diffs, full-state blobs, rebuilding a `Doc`. |
| `katzenqt.tally.events` | `katzenqt.models` | Builders for the five wire messages. Pure. |
| `katzenqt.tally.controller` | persistent, models, all of the above | Owns one in-memory `Doc` per survey, reconciles it with the database, applies inbound events, mutates for local actions. |
| `katzenqt.tally.send` | persistent, models | Stages an outbound tally message onto the conversation's BACAP write stream. |

`katzenqt.tally`'s package namespace re-exports the protocol core only:
`Mode`, `Outcome`, `SlotTally`, `TallyResult`, `apply_vote`, `close_survey`,
`current_version`, `domain`, `new_survey_doc`, `outcome`, `slot_id`,
`slots_of`, `tally`. The controller and the transport carriers are imported
from their own modules so that importing the core stays cheap.

## The document

One `Doc` holds one survey in three root types:

| Root | Type | Contents |
|---|---|---|
| `meta` | `Map` | `survey_id` (hex), `topic`, `mode`, `n_slots`, `status`, and `creator` (hex voter id; absent on surveys created before the field existed). |
| `slots` | `Array` of `Map` | `{id, text}` per slot, in creation order. Fixed at creation. |
| `votes` | `Map` | voter id (hex) → `Map` of `slot_id → availability`, plus the reserved key `_version`. |

Counts are never stored. Roots are read back through `doc.get(name, type=...)`
so that a `Doc` rebuilt from a received update reads identically to a freshly
created one.

The reserved `_version` key inside a voter's map is never a slot id, and the
tally derivation skips it.

## `katzenqt.tally.schema`

```python
class Mode(Enum):
    AVAILABILITY = "availability"   # domain: yes, maybe, no
    APPROVAL     = "approval"       # domain: yes, no

def domain(mode: Mode) -> tuple[str, ...]
def slot_id(index: int) -> str                      # 0 -> "s0"

def new_survey_doc(survey_id: bytes, topic: str, mode: Mode,
                   slots: list[str], creator: bytes | None = None) -> Doc
```

`new_survey_doc` raises `ValueError` if `slots` is empty. `slots` is the list
of descriptive texts; ids are assigned by position. `creator` is the voter id
of whoever opened the survey, recorded so peers can verify that a close came
from the creator.

Accessors, all taking the `Doc`:

```python
def meta_map(doc) -> Map
def votes_map(doc) -> Map
def mode_of(doc) -> Mode
def status_of(doc) -> str                # "open" | "closed"
def survey_id_of(doc) -> bytes
def topic_of(doc) -> str
def creator_of(doc) -> bytes | None      # None on legacy surveys
def slots_of(doc) -> list[tuple[str, str]]   # [(slot_id, text), ...]
```

`votes_map` is the only route to per-voter detail — `engine.tally` aggregates
and does not tell you who voted for what. See the how-to for mapping voter ids
back to peer names.

## `katzenqt.tally.engine`

```python
@dataclass(frozen=True)
class SlotTally:
    slot_id: str
    text: str
    yes: int
    maybe: int      # always 0 in approval mode
    no: int

@dataclass(frozen=True)
class TallyResult:
    survey_id: bytes
    mode: Mode
    status: str
    n_voters: int
    slots: list[SlotTally]

@dataclass(frozen=True)
class Outcome:
    kind: str                  # "winner" | "tie" | "no_winner"
    winners: list[SlotTally]   # empty for "no_winner"
    top_yes: int
```

```python
def apply_vote(doc, voter_id: bytes, choice: dict[str, str], version: int = 0) -> None
def current_version(doc, voter_id: bytes) -> int    # -1 if they have not voted
def close_survey(doc) -> None
def tally(doc) -> TallyResult
def outcome(result: TallyResult) -> Outcome
```

`apply_vote` validates before it writes: every slot id must exist in the survey
and every availability must lie in the mode's domain, else `ValueError`. It
touches only the voter's own key. If `version` is lower than the version
already recorded for that voter, the call is a no-op and the newer vote stands.
Passing an equal version overwrites.

**Counting rules.** `tally` is pure and stores nothing:

- one entry in `votes` is one voter, so `n_voters` is the number of ballots held;
- a voter who omitted a slot counts as `no` for that slot, so
  `yes + maybe + no == n_voters` for every slot;
- `maybe` is only reachable in `availability` mode.

**Outcome rules.** `outcome` looks at yes counts *only* — `maybe` never
contributes:

- no slot has a single yes → `no_winner`, empty `winners`, `top_yes == 0`;
- exactly one slot has the top yes count → `winner`;
- several share it → `tie`, all of them in `winners`.

## `katzenqt.tally.sync`

Thin wrappers over pycrdt's update mechanism (pycrdt 0.13.x argument order:
`get_update` takes the remote state vector positionally).

```python
def state_vector(doc) -> bytes         # compact summary of what doc has
def diff_since(doc, remote_state: bytes) -> bytes
def full_state(doc) -> bytes           # the whole Doc as one update
def apply_update(doc, blob: bytes) -> None
def load_doc(blob: bytes) -> Doc       # rebuild from a full_state blob
```

A joiner or reconnector sends its state vector; a peer replies with the diff
since that vector; the joiner applies it and is caught up in one exchange,
however much it missed. `full_state` is used both to broadcast a new survey and
to persist the survey as one replaceable blob.

`diff_since` requires a *valid* state vector. The vector of an empty document
is `b"\x00"` (`state_vector(Doc())`), and a diff against it is the whole
document; `b""` or anything malformed raises
`ValueError: Cannot decode state` from pycrdt. `handle_event` does not catch
that, so a `TALLY_SYNC_REQ` carrying an empty `crdt` field raises out of the
receive path.

## `katzenqt.tally.events`

Each builder returns a `katzenqt.models.GroupChatMessage` with the explicit
`msg_type` set and a `GroupChatTally` payload. They are pure: no network, no
database. Tally messages are not membership events, so they carry a fixed
32-byte zero `membership_hash`.

```python
def build_create(survey_id: bytes, full_state: bytes) -> GroupChatMessage
def build_vote(survey_id: bytes, choice: dict[str, str], version: int = 0) -> GroupChatMessage
def build_close(survey_id: bytes, version: int = 0) -> GroupChatMessage
def build_sync_request(survey_id: bytes, state_vector: bytes) -> GroupChatMessage
def build_sync_response(survey_id: bytes, diff: bytes) -> GroupChatMessage
```

## Wire format

Tally messages ride the ordinary group chat as CBOR-encoded `GroupChatMessage`
values on the conversation's BACAP write stream, distinguished by `msg_type`:

| `GroupChatTypeEnum` | Value | Payload fields used | Meaning |
|---|---|---|---|
| `TALLY_CREATE` | 5 | `survey_id`, `crdt` = full initial state | A new survey is broadcast. |
| `TALLY_VOTE` | 6 | `survey_id`, `choice`, `version` | A *semantic* vote, not a CRDT update. |
| `TALLY_CLOSE` | 7 | `survey_id`, `version` | The creator declares the survey closed. |
| `TALLY_SYNC_REQ` | 8 | `survey_id`, `crdt` = requester's state vector | Catch-up request. |
| `TALLY_SYNC_RESP` | 9 | `survey_id`, `crdt` = diff since that vector | Catch-up reply. |

```python
class GroupChatTally(BaseModel):
    survey_id: bytes                       # min_length=1
    version: int = 0                       # ge=0
    choice: dict[str, str] | None = None
    crdt: bytes | None = None
```

A vote travels as a *semantic* choice rather than a CRDT update precisely so
the receiver can decide which key it lands under. The receiver writes it under
the **authenticated sender's** voter id, derived from the read capability the
message arrived on — never under an id taken from the payload. That is what
makes "you cannot cast another member's vote" hold without a separate check.

Messages larger than the 1530-byte chunk (a `TALLY_CREATE` for a big survey,
or a `TALLY_SYNC_RESP` carrying a long history) are split by
`models.SendOperation.serialize` across a sub-stream with an indirection entry,
exactly as a large file upload is. This is transparent to tally code.

## Persistence

```python
class TallyState(SQLModel, table=True):
    survey_id: bytes = Field(primary_key=True, min_length=1)
    conversation_id: int = Field(foreign_key="conversation.id", index=True)
    doc_state: bytes
```

One row per survey, overwritten on every mutation; `doc_state` is the whole
`Doc` as a single `full_state` update. Surveys therefore survive a restart and
the sync path has prior state to diff against. The migration is
`0711d7a23cd9_tally_state.py`.

## `katzenqt.tally.controller`

```python
def voter_id_from_read_cap(read_cap: bytes) -> bytes   # blake2b, 16 bytes
```

```python
class TallyController:
    def get(self, survey_id: bytes) -> Doc | None
    def surveys(self) -> list[bytes]
    async def load_all(self) -> None
    async def create_local(self, sess, conversation, survey_id, topic, mode, slots) -> Doc
    async def cast_local_vote(self, sess, conversation, survey_id, choice) -> int | None
    async def close_local(self, sess, conversation, survey_id) -> bool
    async def list_for_conversation(self, sess, conversation_id) -> list[Doc]
    async def handle_event(self, sess, peer, gcm: GroupChatMessage) -> bool

INSTANCE = TallyController()
async def handle_event(sess, peer, gcm) -> bool     # module-level, delegates to INSTANCE
```

The controller has two faces. **Receive**: `handle_event` applies an inbound
message to the local `Doc`. **Send**: `create_local`, `cast_local_vote` and
`close_local` mutate the local `Doc` for the user's own actions; the caller
then transmits the corresponding message.

Notes that matter:

- **The caller owns the transaction.** Every method takes the session and does
  not commit. Nothing is durable until you `await sess.commit()`.
- **`INSTANCE` is process-wide** and is shared by the receive dispatch and the
  headless verbs, so a survey created in one place is visible in the other. Use
  it rather than constructing your own controller, or the two will diverge.
- `load_all` is optional (it opens its own session); the receive path and
  `list_for_conversation` load lazily from `TallyState` on first reference.
- `cast_local_vote` mints the next version itself
  (`current_version(...) + 1`) and returns it, or `None` if the survey is
  unknown. Put that returned version on the outbound `build_vote`. It
  propagates `ValueError` from `apply_vote` for an invalid ballot.
- `close_local` returns `False` if the survey is unknown or the local user is
  not its creator, and logs why.
- `handle_event` returns `True` only when it staged outbound work in the
  session — which today means it answered a `TALLY_SYNC_REQ` — and the caller
  must then poke the send loop.

Receive-side behaviour per kind:

| Kind | Effect |
|---|---|
| `TALLY_CREATE` | Load the `Doc` from the blob, or merge into an existing one. Persist. |
| `TALLY_VOTE` | Record the choice under the authenticated sender's voter id, at the payload's version. An invalid ballot is logged and dropped. A vote for an unknown survey is dropped. |
| `TALLY_CLOSE` | Set status to `closed`, but only if the sender is the recorded creator (or the survey predates the `creator` field). |
| `TALLY_SYNC_REQ` | Stage a `TALLY_SYNC_RESP` carrying the diff since the requester's state vector; returns `True`. |
| `TALLY_SYNC_RESP` | Merge the diff (or load the `Doc` if we had none). Persist. |

## `katzenqt.tally.send`

```python
async def stage_outbound(sess, conversation, gcm) -> uuid.UUID
```

Serialises `gcm` into `PlaintextWAL` rows on the conversation's **`write_cap`**
stream and adds them to the caller's session. Returns the id of the final
`PlaintextWAL`, which lands in `SentLog` once the message has cleared the
network — which is how the CLI knows a send completed.

Outgoing writes must go on `conversation.write_cap`: `find_resendable`
requires the row's `bacap_stream` to match a fully provisioned `WriteCapWAL`,
so any other stream (`own_peer.read_cap_id`, say) silently stalls.

The caller commits, then calls `network.check_for_new()`.

## Routing on receive

`katzenqt.conversation_handlers.dispatch` routes an assembled
`GroupChatMessage` by `msg_type`. The five tally kinds go to the controller and
**never touch `ConversationLog`**, so they do not surface as empty chat lines:

```python
async def dispatch(sess, peer, gcm, full_payload) -> tuple[bool, bool]
    # -> (convlog_added, signal_send)
```

Because `convlog_added` is `False` for tally messages, the network layer does
**not** push onto `network.conversation_update_queue` — see
[Known gaps](#known-gaps).

## Security properties

- **A member cannot cast another member's vote.** The voter id is derived from
  the read capability the message arrived on, so the payload has no say in
  which key it lands under.
- **A member cannot silently rewrite another's ballot**, for the same reason:
  `apply_vote` writes only `votes[voter_id]`.
- **Only the creator can close.** Enforced both locally (`close_local`) and on
  receipt (`handle_event`), against the `creator` recorded in `meta`.
- **Convergence is order-independent.** Concurrent, disjoint votes merge
  symmetrically; a late joiner catches up in one state-vector exchange. This is
  a property test (`tests/test_tally_convergence.py`) as well as a unit test.

### What is not enforced

- **Closing is advisory.** It sets `status` to `closed`; it does **not** prevent
  further votes, and a ballot cast after a close is still counted. Enforcing
  "no votes after close" convergently across peers needs causal ordering and is
  future work.
- **Membership is not checked.** Any peer of the conversation whose message
  reaches the receive path can vote in any survey that peer knows about; there
  is no separate check that they were a member when the survey was created.
- **Versions are per-voter intent order, not a clock.** They order one voter's
  own recasts. They say nothing across voters.
- **Voter identity depends on a provisioned read capability.** If a peer's read
  cap has not been provisioned yet, `_voter_id` logs a warning and falls back to
  hashing the peer's local `read_cap_id` UUID so a vote is not silently
  dropped in tests. That fallback id is *local only* — other peers will not
  derive it — so a ballot cast in that window will not merge with the same
  voter's ballots elsewhere.

## The headless CLI

Installed as `katzenqt-headless` in the venv. Every connecting verb requires
exactly one of `--config <thinclient.toml>` or `--address <addr>` (with
optional `--network {tcp,unix}`, default `tcp`); there is no filesystem search.
`KQT_STATE` selects the identity's state file. All output goes to **stderr**;
match tokens by substring.

| Verb | Arguments | Success output |
|---|---|---|
| `tally-create` | `conv_name` `topic` `[--mode approval\|availability]` `--slot TEXT` (repeat) | `SENT`, then `TALLY_CREATED=<survey_id hex>` |
| `tally-vote` | `conv_name` `--survey HEX` `--slot SLOT_ID=availability` (repeat) `[--timeout 600]` | `VOTED` |
| `tally-result` | `conv_name` `--survey HEX` `[--expect-voters N]` `[--timeout 600]` | `TALLY=<json>`, then `WINNER=…` / `TIE=…` / `WINNER=none (no yes votes)` |
| `tally-close` | `conv_name` `--survey HEX` `[--timeout 600]` | `CLOSED` |
| `tally-list` | `conv_name` — **offline, no connection argument** | one `SURVEY=<hex> status=… voters=N mode=… topic=<json>` per survey, or `(no surveys)` |

`--mode` defaults to `approval`. `tally-vote` and `tally-close` first wait
(polling `TallyState` every 0.5 s, up to `--timeout`) for the survey to arrive,
then stage the message and wait up to `max(120, timeout)` seconds for it to
reach `SentLog` — all on one connection, because a process may run only one
connect/shutdown cycle. `tally-result` polls until the survey has at least
`--expect-voters` voters; on timeout it still prints the latest tally it saw
before failing.

Exit codes:

| Code | Meaning |
|---|---|
| 0 | Success. |
| 1 | Timed out waiting for the survey or the expected voters; or the close was refused (not the creator); or the vote could not be applied. |
| 2 | Usage or lookup error: unknown mode, malformed `SLOT_ID=availability`, invalid ballot, conversation not found. |
| 3 | The message was staged but did not reach `SentLog` in the budget. |

`TALLY=` carries this JSON:

```json
{
  "survey_id": "…hex…",
  "mode": "approval",
  "status": "open",
  "n_voters": 2,
  "slots": [{"slot_id": "s0", "text": "Pizza", "yes": 2, "maybe": 0, "no": 0}],
  "outcome": "winner",
  "winners": [{"slot_id": "s0", "text": "Pizza", "yes": 2}]
}
```

## Known gaps

Each of these has a workaround, where one exists, in
[tally-howto.md](tally-howto.md).

1. **No change notification.** Applying an inbound tally event updates
   `TallyState` and the in-memory `Doc` but signals nothing — the chat's
   `network.conversation_update_queue` is only poked for `ConversationLog`
   rows. A view must poll, or the receive path must gain a queue of its own.
2. **The catch-up request is never sent.** `events.build_sync_request` exists
   and the receive side answers `TALLY_SYNC_REQ` correctly, but nothing calls
   the builder, so a peer that joins after a survey was created only learns
   about it if someone re-broadcasts. Wiring the request is a GUI-visible
   feature ("refresh this survey").
3. **Closing does not stop voting** (see above).
4. **No per-voter view in the derived result.** `TallyResult` aggregates;
   showing "who voted for what" means reading `schema.votes_map` directly.
5. **A malformed sync request raises out of the receive path.**
   `handle_event` passes `tally.crdt or b""` straight to `diff_since`, which
   rejects an empty vector with `ValueError` rather than treating it as "send
   me everything".

## Tests as documentation

| File | What it pins down |
|---|---|
| `tests/test_tally_engine.py` | Counting rules, domain validation, versioning, outcome declaration. |
| `tests/test_tally_sync.py` | Late-joiner catch-up, symmetric merge of concurrent votes. |
| `tests/test_tally_convergence.py` | Property test: event order never changes the tally. |
| `tests/test_tally_controller.py` | Votes keyed to the authenticated sender, persistence round-trip, creator-only close, dispatch routing. |
| `tests/test_models_tally.py` | CBOR round-trip of every kind, integer `msg_type` on the wire, large CRDT blobs through `SendOperation`. |
| `tests/integration/test_tally.py` | The whole path over a docker mixnet, two peers, convergent counts. |
