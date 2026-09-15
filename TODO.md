# katzenqt / katzenpost TODO

## Maintenance rules for this file

This file is a living document and is committed to git (branch `deckard-dev`).
Keep it up to date as part of the working session:

- Whenever a TODO item is completed, mark it done (move it to a "Done" /
  "Resolved" section or update its status) and **commit** the change.
- Whenever new discoveries obsolete or supersede statements in this file,
  update the statements and **commit**.
- Whenever new tasks surface, add them (either marked as new/backlog or with a
  note about their priority) and **commit** if they are "session-worthy".
  Small ephemeral notes that will be consumed within the same session can
  stay uncommitted until the session ends.
- Commit messages should be short and match the repo's existing style
  (imperative, lowercase-ish, e.g. `add TODO.md: ...`, `update TODO.md: ...`).
- Keep TODO.md commits **separate** from commits that change other files
  (code, tests, etc.). When completing an item, first commit the work itself,
  then commit the TODO.md status update. This keeps the non-TODO commits
  cherry-pickable on their own.

## Code hygiene: comments and docstrings

Applies to every comment and docstring added/changed while working the items
below (exemplar: commit `103a209` "comments: describe current behavior, not
the bugs they guarded").

- Describe what the code **does now**, not the bug it guards against or the
  implementation it replaced. Root-cause narratives, "used to ..." notes, and
  tuning/justification history belong in **commit messages**, not in the code.
- Never reference `TODO item N` (or the plan's item numbers) in code,
  docstrings, tests, or migration headers. The numbers are bookkeeping for
  this file alone and go stale as the plan moves; readers of the code in the
  distant future should not need this file to understand it.
- Write for a reader who sees only the current code: current behavior,
  current invariants, current recovery contracts. A comment that only makes
  sense alongside a past state is dead weight after the next refactor.
- In tests, the docstring states the behavior under test. Where several tests
  share a caveat (e.g. the reconnect-marker swap), state it once concisely;
  do not cross-reference "as in test X".
- Loose `TODO:` queries left with no plan number are fine where they mark a
  genuine open question, but keep them specific and current.

State as of 2026-09-14. Session context: recovering from a proxy-sweep storm in a
5-replica katzenpost mixnet while debugging the delivery of Bob's
`jamiroquai.webp` (37300 B) to Alice and Carol. **Delivery of Bob's second
send of jamiroquai.webp has been CONFIRMED to both Alice and Carol** (MD5
`00c8541c15e56ff317c15e755a97427e` verified identical to source). The items
below remain open.

Also as of 2026-09-14: deckard-dev merged `origin/main` (PR #66 "Harden peer
input and local state", commit `243422a`) via merge commit `27fbe52`, taking
main's reconnect/epoch-marker anti-race RPC guard (`_rpc_racing_connection_life`,
mark_sent `resolve_counter`, pause/resume-friendly `rcr=None`), the voucher
handshake's bounded daemon RPCs, `_substream_parent` + `test_substream_guard.py`
(peer-name spoofing guard, see item 2), the `KQT_SEND_BUDGET_FLOOR_S` CI budget
floor, and the junit/`--no-cov` CI reporting. Full suite: 403 passed / 14
skipped.

Quick orientation for a new session:

- Repo: `/home/kpdev/katzenqt` (Python client). Sibling Go repo:
  `/home/kpdev/katzenpost` (mixnet / replicas / courier).
- Client code of interest:
  - `src/katzenqt/network.py` — read/write drain loops, `_try_assemble`,
    `drain_mixwal_read_single`, substream handling, `_SUBSTREAM_NAME_PREFIX`
    at `network.py:303`, `_substream_parent` at `network.py:472`.
  - `src/katzenqt/persistent.py` — MixWAL / PlaintextWAL / ReadCapWAL /
    ConversationPeer / ReceivedPiece models, `get_resendable()` gates
    (`after_id` / `after_stream` around `persistent.py:676-740`).
  - `src/katzenqt/models.py` — `serialize()` at `models.py:82-170`
    (substream creation, `agg_bacap_stream`).
  - `src/katzenqt/katzen.py` — GUI backend; `add_conversation()` at
    `katzen.py:1827-1864`.
  - `src/katzenqt/voucher.py` — `conversation_is_joined()` at `voucher.py:62-73`.
- Mixnet replica code: `/home/kpdev/katzenpost/replica/handlers.go`,
  `/home/kpdev/katzenpost/replica/proxy_request_manager.go`,
  `/home/kpdev/katzenpost/replica/connector.go`.
- Dockerized testnet: `/home/kpdev/katzenpost/docker/mixnet-alpine/`.
  Makefile targets in that dir: if a full containerized-mixnet restart is ever
  needed, pass `base_port=62331` so kpclientd lands on `127.0.0.1:64331`.
- The webtop container (runs the 3 client GUI apps) is `katzenqt_webtop`.
  Client DBs LIVES **inside webtop**, not on the host:
  `/config/.local/share/katzenqt/{a,b,c}.sqlite3` (WAL mode; read with the WAL
  present; owner uid 1001 `abc:abc`). Logs: `/config/katzenqt/{a,b,c}.log`
  (client local time = UTC+2; host/replica logs are UTC).
- kpclientd container: `mixnet-alpine_da39a-kpclientd-1`, runs
  `/mixnet-alpine/kpclientd.alpine -c /mixnet-alpine/client/client.toml` on
  `127.0.0.1:64331`, epoch 2m.
- Clients are relaunched in webtop as user `abc` via
  `/config/katzenqt/start.sh` (`KQT_STATE=a|b|c uv run katzenqt`). To launch
  programmatically, use `podman exec -e DISPLAY=:0 -e
  WAYLAND_DISPLAY=wayland-0 -e XDG_RUNTIME_DIR=/config/.XDG -e
  QT_QPA_PLATFORM=wayland -e DBUS_SESSION_BUS_ADDRESS=... -u abc`; otherwise the
  system tray is unavailable and `window.systray` stays `None`, which crashes
  `conversation_selected` at `katzen.py:1500`
  (`'NoneType' object has no attribute 'has_read_messages'`).

---

## 1. Investigate the katzenpost replica proxy-sweep storm (root cause bug)

Investigate and fix the replica-side proxy storm that has been hammering this
5-replica testnet and was the root cause of the failed first delivery.

### Symptoms / evidence

- Tens of thousands of `proxy sweep budget exhausted` errors in replica logs:
  `replica1=~72,800`, `replica3=~35,000`, `replica4=~25,300`, `replica5=~34,000`
  (paths: `/home/kpdev/katzenpost/docker/mixnet-alpine/replica{1..5}/katzenpost.log`).
- Replicas stuck at 50-85% CPU for days, even today while reads were still
  completing on the second attempt.
- `err=9` (ReplicationFailed) <-> `err=1` (BoxIDNotFound) churn in
  `/home/kpdev/katzenpost/docker/mixnet-alpine/servicenode{1,2,3}/courier/courier.log`.
- Restarting the 5 replicas dropped CPU 50% -> 3-4% for ~2 min, then it crept
  back to 26-85% as the clients' dead-substream read loops re-engaged. Removing
  the dead-substream reads (see item 3) gave steady ~1 crate/min progress with
  only transient stalls.

### Code to inspect

- `/home/kpdev/katzenpost/replica/handlers.go`:
  - `proxySweepBudget` / `proxyAttemptTimeout` around `handlers.go:627-656`;
    `errProxySweepBudgetExhausted` at `handlers.go:662`.
  - The proxy-failover read path (`proxyReadRequest`, "trying the next holder",
    `errorCodeProxyReadTimeout` etc). A recently-rewritten proxy failover
    segment is a strong suspect (self-inflicted probe loops between replicas).
- `/home/kpdev/katzenpost/replica/proxy_request_manager.go` — proxy worker slot
  allocation at `proxy_request_manager.go:125` (`ProxyWorkerCount=0` default?).
- `/home/kpdev/katzenpost/replica/connector.go` — replication dispatch / queue
  for retry at `connector.go:334-388` ("Only dispatched to M/N targets (others
  queued for retry)").
- `/home/kpdev/katzenpost/pigeonhole/errors.go` — replica error codes 0-11.
- Replica `replica.toml` currently uses `ProxyRequestTimeout = 0` and
  `ProxyWorkerCount = 0` (inferred defaults); question whether 0 is actually a
  sane default and whether these need explicit non-zero values.

### Suspects to pursue

1. Infinite/long-lived proxy probe loop when a box genuinely does not exist
   durable, amplified by every client retrying the read (client-side
   `no_retry_on_box_id_not_found=False` in the kpclientd config means rides out
   BoxIDNotFound forever instead of giving up).
2. Replication acknowledged to the client before durability (see item 3) so the
   proxy machinery spins trying to satisfy reads for boxes that will never
   appear.
3. `ProxyWorkerCount` / `ProxyRequestTimeout` defaults of 0 interacting badly
   with the rewritten proxy manager.

---

## 2. Fix: `:substream:` synthetic peers leak into the GUI user list

The synthetic substream peers appear in the contact/user list in the GUI.

### Where

- `src/katzenqt/network.py:303` defines `_SUBSTREAM_NAME_PREFIX = ":substream:"`.
- Substream peers are created with names like `:substream:3:64c9` on I-chunk
  receive (`network.py:755-761`, `active=True`), and retired (`active=False`) on
  F-assembly (`network.py:734`), but are **never deleted** from the DB.
- GUI lists contacts from the DB without filtering: `add_conversation()` at
  `katzen.py:1848-1851` iterates ALL `convo.peers` and thus shows
  `:substream:2:77ca`, `:substream:3:f8f2`, etc. in the user list of Alice's /
  Carol's GUI.
- Contrast: `voucher.py:68` (`conversation_is_joined()`) and `_actions.py`
  correctly exclude `:substream:` names, so the leak is purely a display bug.

### Options

- Filter out `name.startswith(":substream:")` (and/or inactive peers) where the
  user list is rendered (`katzen.py:1848-1851`).
- Optionally actually delete or tombstone retired substream peers instead of
  leaving `active=False` rows forever. (Note item 3 and the DB surgery — stale
  substream peer rows caused real damage; housekeeping here is non-trivial
  because the `active` flag is also used to stop reads.)

### Related commit (now merged) and post-merge state

- `a3d2e2bc44b` "Stop a peer name from spoofing a substream and crashing the
  reader" has been **merged into deckard-dev** via main's PR #66
  (`243422a`/merge `27fbe52`) as `_substream_parent` in network.py
  (`network.py:472`) + `tests/test_substream_guard.py`. It stops a peer NAME
  like `:substream:` from spoofing a substream — but it does NOT filter
  `:substream:` peers out of the GUI user list.
- **Still open:** `add_conversation()` at `katzen.py:1959-2016` iterates ALL
  `convo.peers` and renders `:substream:*` names into the contacts tree. The
  fix is to skip `peer.name.startswith(network._SUBSTREAM_NAME_PREFIX)` (and/or
  `not peer.active`) there, mirroring the exclusions already in
  `voucher.py:77` and `headless/_actions.py:750-753`. Optionally also
  delete/tombstone retired substream peers instead of leaving
  `active=False` rows forever (see item 3's DB-surgery lessons).
  **DONE in `d8f943d` (code) + `0510097` (tests)** — see status below.

### Status (complete, 2026-09-14)

DONE in `d8f943d`:
- Extracted module-level `_peer_is_displayable()` (katzen.py:73) keyed on
  `network._SUBSTREAM_NAME_PREFIX`; applied in `add_conversation` peer loop
  (katzen.py:1992).
- Defensive prefix guard in `_process_peer_added` (katzen.py:1261), the
  `_await_voucher_join` appendRow (katzen.py:1794), and
  `induct_via_voucher` appendRow (katzen.py:1871).
- `voucher._build_who_reply` (voucher.py:677) now skips substream peers.

Tests DONE in `0510097` (`tests/test_substream_gui_filter.py`): predicate unit
tests (normal yes / substream no / inactive substream no / near-miss name
yes) + who-reply skips an active substream peer while keeping real members.

NOTE: filtering substream rows removes today's only GUI handle for pausing
dead substreams; TODO item 4's Transfers panel replaces that handle (see
item 4 plan below).

---

## 3. Underlying bug that forced DB surgery: dead-substream read amplification (a.k.a. the "replica storm feedback" bug)

Root cause analysis of why Bob's FIRST jamiroquai.webp send failed and why DB
surgery was required to let the SECOND send through. This is the most
important thing to understand before touching item 1.

### Chain of events (observed)

1. On the first send, the client received ACKs from kpclientd for all file
   chunks (e.g. boxes 5519-5525), but boxes ~5526-5543 (18 of 25 chunks) were
   **never durably stored** in the replicas. `BoxIDNotFound` on all replicas
   for that range, permanently. (Blame roller: replication acknowledged before
   durability / replication failures during the storm.)
2. Because each file send gets a **fresh substream** (new
   `agg_bacap_stream` UUID + write/read caps starting at index 0), the lost
   boxes were unrecoverable: there is no way to re-fetch what was never stored,
   and no retry that can resurrect them.
3. The client DB had **no way to forget** the dead substream. `drain_mixwal_read_single`
   (with kpclientd `no_retry_on_box_id_not_found=False`) read loops rode
   `BoxIDNotFound` **forever**, re-issuing reads every few seconds for streams
   that could never yield data. With Bob + Alice + Carol all reading dead
   substreams and ticking the proxy machinery, the replica storm went critical:
   67k+ `proxy sweep budget exhausted`, 5 replicas at ~50% CPU for 3 days.
4. **DB surgery that fixed it** (documented so it can be re-derived, not
   recommended as a permanent fix):
   - Stopped the 3 katzenqt apps + stopped kpclientd.
   - In webtop DBs, for each dead substream peer
     (`conversationpeer.name LIKE ':substream:%'`, rows id=4 named
     `:substream:2:77ca` / `:substream:3:f8f2`): set `active=0`, and deleted the
     matching `mixwal` row whose `bacap_stream` was the dead readcap
     UUID (`538ae279` for Alice, `99614674` for Carol).
   - Copied DBs back into webtop, fixed ownership to `abc:abc`.
   - Backups exist in webtop as `{a,b,c}.sqlite3.bak_disab` /
     `*.sqlite3-wal.bak_disab`.
   - Effect: clients stopped reading the dead substreams; the residual storm
     subsided enough for Bob's second send (fresh substream base
     `3256359971903960859`, I-chunk on main stream at
     `4007537799310947422`) to drain 860->883 in both Alice and Carol and
     deliver the file.

### Schema facts needed to reason about this

- `conversationpeer`: `id/name/active/read_cap_id`; `mixwal`:
  `bacap_stream/current_message_index`; `readcapwal`:
  `id/read_cap/next_index`. `bacap_stream` stored as 32-hex, no dashes.
- `current_message_index` / `next_index` are 104-byte blobs; the first 8 bytes
  are the LE uint64 Pigeonhole box index (BACAP counters).
- Substream write/read caps start at their own index 0.

### Open questions / likely fix surfaces

RESOLVED DURING REVIEW (2026-09-11) and IMPLEMENTED (2026-09-13, commit
`3bba25a`):

- **What the writer's ACK means (answered from Go code):** a write completes on
  the courier's ACK — "a single mixnet round trip" (`client/thin/pigeonhole.go:452`,
  `client/arq.go:84-99`: idempotent write + `ReplyType=ACK` -> `ARQActionComplete`).
  The courier's `ackReply` fires "the moment an envelope is accepted. Replica
  dispatch happens asynchronously" (`courier/server/plugin.go:523-528`) and is
  fire-and-forget to the 2 intermediate replicas, with NO courier-level retry on
  the normal write path. So the writer's ACK means **only "a courier cached the
  envelope" — not that any replica durable-stored it**, and the write client is
  ACK'd and gone (cache-based redispatch, which only fires on client re-polls,
  never runs). BUT the reader's not-found is a much stronger signal: reads never
  consult the courier cache, they hit the shard replicas through the proxy
  failover (`readBoxFromShardReplicas`/"trying the next holder"), so a
  `BoxIDNotFound` reaching the reader means **no holder anywhere can serve it**.
- **First not-found IS terminal → deactivate on the FIRST not-found, no N
  counter needed.** Even with async dispatch, the I-chunk is gated
  `after_stream` (only written after all substream boxes ACK'd at couriers), the
  reader only learns of the substream after the I-chunk survives a full mixnet
  round trip, plus app-facing delays (5s give_up sleep, 15s sweep, 60s arming
  sweep) stack on top — any replica dispatch still in flight has landed or
  permanently failed long before the reader first tries the substream box. This
  matches operationally: boxes 5526-5543 never reappeared.
- **Design confirmed:** in `drain_mixwal_read_single`, for peers whose name
  starts with `_SUBSTREAM_NAME_PREFIX`, use `no_retry_on_box_id_not_found=True`
  (fail-fast, like `voucher._read_box`); on the first `BoxIDNotFoundError`/
  `TombstoneError`, cancel the in-flight ARQ (`cancel_resending_encrypted_message`)
  and drain task (new per-`bacap_stream` registry), set `cp.active=False`,
  delete the is_read MixWAL row, `draining_right_now.discard`, and WARNING-log.
  Keep the ReadCapWAL + ReceivedPiece rows so a future retry (item 4/5) can
  resume from `next_index`. Normal conversation peers keep the current
  ride-out behavior.
- Are the items above (deactivate/keep-RP/retry primitive) consistent with item
  5's per-peer pause/resume? Yes — pause/deactivate share the same machinery.

DONE in `3bba25a`: `drain_mixwal_read_single` sets
`no_retry_on_box_id_not_found=True` for substream peers and on the first
`BoxIDNotFoundError`/`TombstoneError` deactivates `cp.active`, deletes the
is_read MixWAL row, cancels the in-flight read ARQ + drain task (per-
`bacap_stream` registry `_inflight_reads`), discards from `draining_right_now`,
and WARNING-logs; `InvalidEpochError` added to the transient-recover catch
(needed because `no_retry=True` surfaces it as a `ReplicaError` subclass that
was previously uncaught). Normal conversation peers keep the 5s ride-out.
ReadCapWAL + ReceivedPiece rows are kept so item 5's resume can re-arm.

---

## 4. Feature: show substream file-download status in the GUI, with pause/cancel

Currently, when a recipient's main stream yields an `I`-chunk announcing "there
is a file on substream X, download it starting at index 0", the client silently
creates the substream peer and starts draining it. The GUI shows nothing until
the whole file arrives. Feature request (addition, not a bugfix):

- Show an in-UI indication that a file is being downloaded from a substream,
  including its status/progress (pieces received vs total known; see
  `models.serialize()` for chunk counts, `network.py` `_try_assemble`,
  ReceivedPiece rows).
- Give the recipient the ability to **pause** and **cancel** the substream
  download while **continuing to receive/read messages on the main stream**.
  (Currently the read loops are driven by per-MixWAL `drain_mixwal_read_single`
  coroutines keyed on `bacap_stream`, so pause/cancel must target the substream
  MixWAL/WAL entry without touching the main-stream entries — see
  `persistent.MixWAL`, `draining_right_now` set, `readables_to_mixwal()`.)
- Design decisions to surface: how to represent the in-progress file in the QML
  UI model; what "pause" semantically means for a BACAP index (freeze the
  `next_index` cursor and stop the coroutine vs mark the MW and skip in
  `get_resendable()`/`readables_to_mixwal()`); whether cancel should prune
  ReceivedPiece rows + retire the substream peer + delete its MixWAL (mirroring
  the DB surgery in item 3, but done cleanly in-app; possibly also tombstone the
  substream's remaining boxes, which would require the write cap... likely out
  of scope).

### Plan (2026-09-14, COMPLETE 2026-09-15)

Two design facts from the 2026-09-14 review of the post-merge code:
- **No denominator today.** The I-chunk wire body is only `b'I' + 136-byte read
  cap` (`network.py:1546`); the sender computes the chunk count in
  `models.serialize()` (`models.py:134-151`) but never transmits it, so
  "pieces received vs total" is unknowable at read time. Fix: extend the
  I-chunk to `b'I' + struct.pack(">I", total_chunks) + read_cap` (140-byte
  body); receiver parses both the legacy 136-byte (total unknown →
  indeterminate) and new 140-byte forms. Sender total count stored on the
  indirection `ReadCapWAL` as a new nullable `substream_total_chunks` column
  (needs an Alembic migration).
- **Progress numerator already exists:** `COUNT(ReceivedPiece WHERE
  read_cap == <substream rcw.id>` (`network.py:940-945` inserts one row per
  box; `headless/_actions.py:772-774` already counts them).

Shipped surface (per decisions on 2026-09-14):
- **Transfers panel** (not in-chat rows): a new `DownloadsModel`-backed
  `QTableView` under the contacts tree listing resumable substream downloads
  (column: contact, status downloading/paused, `pieces/total` or
  indeterminate), with a right-click Pause/Resume menu.
- **Pause ONLY; Cancel deferred.** `pause_peer_reads` / `resume_peer_reads`
  (item 5) already freeze/resume exactly one stream — cancel (prune
  ReceivedPiece + retire peer + delete MixWAL) is out of scope for now.
- **Network→GUI events** via a new module-level
  `network.substream_progress_queue` (mirroring `conversation_update_queue`)
  plus a `transfers_listener()` coroutine in katzen.py `main()`:
  `started` / `piece` / `completed` / `paused` / `resumed` events keyed by
  rcw_id. Startup seeds the panel from active-or-resumable substream
  `ConversationPeer` rows (active OR has ReceivedPiece), which also replaces
  the pause/resume handle for dead substreams that item 2's filter removes
  from the contacts tree.

### Step 2.1 — wire total (DONE in `346ba4f`, 2026-09-14)

- `ReadCapWAL.substream_total_chunks: int | None` (persistent.py:353) +
  Alembic migration `c4f1a8b2e9d7` (down_revision `d08418a855a1`). Verified
  via `tests/migrations/test_upgrade.py` (all revisions reach head) and the
  extension test in `tests/test_network_fake.py`.
- `models.serialize()` sets `substream_total_chunks = C_chunk_count + 1` on
  the indirection `ReadCapWAL` (models.py:158-165).
- Send side (`network.py:1570-1573`): legacy 136-byte `b'I'+read_cap` when the
  sender's rcw predates the column (total None); extended
  `b'I' + struct.pack(">I", total) + read_cap` (140 B) otherwise. Existing
  `test_indirection_pwal_fills_read_cap_before_dispatch` now pins the
  fallback; new `test_indirection_pwal_prepends_total_chunk_count_when_known`
  pins the extended form.
- Receive side (`network.py:1030-1048`): accepts 136 (total unknown) and 140
  (bytes 0-3 = total, bytes 4-139 = read cap) forms; malformed lengths still
  warning-and-ignore. `substream_total_chunks` persisted on the receiver's
  new_rcw for the GUI denominator.

### Step 2.2 — progress events queue (DONE in `11cd56c`, 2026-09-14)

- New module-level `network.substream_progress_queue` (network.py:92) holding
  post-commit events as `(kind, ...)` tuples:
  `("started", rcw_id, conv_id, total_or_None, parent_name)`,
  `("piece", rcw_id, count)`, `("completed", rcw_id)`, `("paused", rcw_id)`,
  `("resumed", rcw_id)`.
- Push sites: I-branch create (`started`), per-substream ReceivedPiece insert
  (`piece`, via a `COUNT(ReceivedPiece WHERE read_cap == mw.bacap_stream)` in
  the same unflushed transaction), substream terminal-F retire (`completed`),
  and `pause_peer_reads`/`resume_peer_reads` when the peer is a substream.
- All events are held until the commit succeeds, so listeners never observe
  uncommitted pieces (OperationalError retries roll back both the rows and
  the pending events).

### Step 2.3 — Transfers panel (DONE in `b80e715`, 2026-09-15)

- `qt_models.DownloadsModel(QAbstractTableModel)`: rows keyed by ReadCapWAL id;
  columns Contact / Progress / State, plus structured roles
  `ROLE_TRANSFER_RCW_ID` (0x200) and `ROLE_TRANSFER_*` for pieces/total/active.
  Methods `start_transfer` (insert or refresh unknown total),
  `notify_piece`, `complete_transfer` (remove row), `set_paused`.
- `MainWindow.__init__` builds `transfers_model` + `transfers_view`
  (QTableView, gridLayout_2 row 2, under the contacts tree) with a custom
  context menu; `transfers_context_menu` toggles Pause/Resume via the item-5
  `network.pause_peer_reads`/`resume_peer_reads` primitives.
- `transfers_listener()` mirrors `receive_msg_listener` (queue.get on the IO
  thread, model mutation on the Qt thread) and translates
  started/piece/completed/paused/resumed events into model calls.
- `main()` starts `transfers_listener()` and seeds the panel from the DB with
  `await transfers_model.seed_from_db()` (resumable = active peer or
  has ReceivedPiece rows; parent display name via `_substream_parent_name`,
  hiding the synthetic `:substream:` peer name).

### Step 2.4 — tests (DONE in `08a253b`, 2026-09-15)

- `tests/test_downloads_model.py` (8, offscreen QGuiApplication): inserts
  unknown-total UI rows in proportion to `substream_total_chunks`, refresh
  jumps to the larger denominator, `notify_piece` increments state text, the
  final piece keeps the target visible, unknown ids are ignored, `complete`
  removes rows, `set_paused` toggles the State column + active role,
  column/role header metadata, and `seed_from_db` restores resumable transfers
  (filters active peers / peers with ReceivedPiece rows).
- `tests/test_network_fake.py` receive-side events: extended I-chunk (140 B)
  persists `substream_total_chunks` and its `started` event carries
  (rcw, conv_id, total, parent_name); legacy 136-byte `started` carries
  total None; each substream C-chunk read fires `("piece", rcw, count)`; the
  terminal F assembles through the parent peer resolved from the substream
  name and fires `("completed", rcw)`. `TestPauseResumePeerReads` now asserts
  the `paused`/`resumed` events.
- `tests/test_listener_hardening.py` `TestTransfersListenerDrainsEvents`:
  `transfers_listener` dispatches all five event kinds to `DownloadsModel`
  and, like the other UI listeners, survives a per-item error (log-and-
  continue). This test exposed two fixes shipped in `08a253b`: the missing
  `DownloadsModel._idx` helper, and the missing try/except wrapper in
  `transfers_listener` itself.
- `tests/test_models.py` `test_serialize_sets_substream_total_chunks_on_multi_chunk`
  pins the C-chunks+1 denominator on multi-box sends.
- `tests/conftest.py`: the state-reset hook now drains
  `network.substream_progress_queue` so module-level events cannot leak
  across tests.
- Still deferred to item 5: a GUI test driving the actual
  `pause_peer_reads`/`resume_peer_reads` calls from the panel's context
  menu.

---

## 5. Feature: per-peer pause/resume (and the retry primitive for dead substreams)

The `ConversationPeer.active` flag (`persistent.py:762`) already gates arming
(`network.py:1221`), but there is no GUI way to toggle it individual peers, and
`active=False` alone is not enough to stop reads: `drain_mixwal2`'s 15s sweep
re-casts pending is_read MixWAL rows regardless of `active`. Building
per-peer pause/resume:

- Pause a peer = set `active=False`, delete its pending is_read MixWAL rows,
  and cancel any in-flight read ARQ (`cancel_resending_encrypted_message`) and
  drain task (shared per-`bacap_stream` registry from item 3), so reads stop
  immediately rather than on the next sweep.
- Resume = set `active=True` and poke `readables_to_mixwal_event` so the read
  arms again from the saved ReadCapWAL `next_index`. Kept ReceivedPiece rows
  are picked up by `_try_assemble`.
- GUI: tag contacts-tree peer `QStandardItem`s with `peer.id` (Qt.UserRole;
  currently only convo items carry `conversation_id` at `katzen.py:1833`, peer
  items have no id) and add a right-click Pause/Resume action on peer rows
  (not own-peer; `katzen.py:1536`). QMenu already imported at `katzen.py:34`.
- This is also the retry primitive item 4's download pause/cancel and the
  dead-substream resume button need — a deactivated substream (item 3) can be
  re-armed via Resume.
- Note: `a3d2e2bc44b` (see item 2) was merged via main's PR #66 and now
  supplies `_substream_parent`, which the merged code already uses to route
  substream reads back to their parent peer.

DONE in `3bba25a`:
- Network level: `pause_peer_reads(bacap_stream)` / `resume_peer_reads(bacap_stream)` —
  pause cancels the in-flight drain task (via `_inflight_reads` registry) and ARQ
  (`cancel_resending_encrypted_message`), deletes the is_read MixWAL rows, sets
  `cp.active=False`, discards from `_inflight_reads`/`__resend_queue`, pokes the
  events; resume sets `active=True` and pokes `readables_to_mixwal_event` so the
  read re-arms from the saved ReadCapWAL `next_index`.
- GUI: `add_conversation`/`_process_peer_added` tag peer `QStandardItem`s with
  `peer_read_cap_id` + `peer_is_own`; `contacts_treeWidget` context menu
  (`peer_context_menu`) offers "Do not read from X any more" / "Resume reading
  from X" (own-peer row excluded and skipped).
- Tests: `TestPauseResumePeerReads` (cancel-in-flight, deactivate-without-task,
  resume-rearms-from-saved-index); full suite 307 passed / 14 skipped.

Item 4's pause/cancel (substream download progress in GUI) remains open — only
the per-peer machinery it needs is now in place.