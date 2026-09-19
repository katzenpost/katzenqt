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

---

## Quick orientation for a new session

- Repo: `/home/kpdev/katzenqt` (Python client). Sibling Go repo:
  `/home/kpdev/katzenpost` (mixnet / replicas / courier).
- Client code of interest:
  - `src/katzenqt/network.py` — read/write drain loops, `_try_assemble`,
    `drain_mixwal_read_single`, `drain_mixwal_write_single`, substream
    handling, `_SUBSTREAM_NAME_PREFIX`, `_substream_parent`, the
    `pause_peer_reads` / `resume_peer_reads` primitives, and
    `substream_progress_queue`.
  - `src/katzenqt/persistent.py` — MixWAL / PlaintextWAL / ReadCapWAL /
    WriteCapWAL / ConversationPeer / ReceivedPiece models, and
    `PlaintextWAL.find_resendable()` (the `after_id` / `after_stream` gates).
  - `src/katzenqt/models.py` — `SendOperation.serialize()`: chunk splitting,
    substream (`agg_bacap_stream`) creation, the indirection ReadCapWAL and
    the I-chunk PlaintextWAL.
  - `src/katzenqt/katzen.py` — GUI backend: `MainWindow`, the `@async_cb`
    actions, the supervised listeners, `add_conversation()`.
  - `src/katzenqt/qt_models.py` — `ConversationUIState`, `DownloadsModel`,
    and the chat/transfer model roles.
  - `src/katzenqt/voucher.py` — the contact-voucher handshake
    (`conversation_is_joined()`, `mint_and_publish`, `derive_read_and_induct`).
- Environments:
  - Host tests use the repo-local `.venv` (uv, CPython 3.13, editable
    katzenqt + `~/thin_client`); the shared webtop venv is no longer used.
    `uv run pytest --no-cov` runs the unit suite; `tests/integration` needs
    the docker mixnet up.
  - The webtop GUI clients use a separate container venv,
    `/config/.venv-katzenqt`.
- Mixnet replica code: `/home/kpdev/katzenpost/replica/handlers.go`,
  `/home/kpdev/katzenpost/replica/proxy_request_manager.go`,
  `/home/kpdev/katzenpost/replica/connector.go`.
- Dockerized testnet: `/home/kpdev/katzenpost/docker/mixnet-alpine/`.
  **Runs under rootless podman + podman-compose, not Docker**: `podman ps`,
  `podman stats`, `podman top <ctr>` all work from the host without sudo.
  Makefile: `/home/kpdev/katzenpost/docker/Makefile`; if a full
  containerized-mixnet restart is ever needed, pass `base_port=62331` so
  kpclientd lands on `127.0.0.1:64331`.
- The webtop container (runs the 3 client GUI apps) is `katzenqt_webtop`, with
  the repo mounted read-write at `/config/katzenqt`. Client DBs live **inside
  webtop**, not on the host: `/config/.local/share/katzenqt/{a,b,c}.sqlite3`
  (WAL mode; read with the WAL present; owner uid 1001 `abc:abc`; for
  read-only inspection use `sqlite3.connect("file:...?mode=ro", uri=True)`).
  Spilled attachments land in
  `/config/.local/share/katzenqt/attachments/<conversation_id>/`, a directory
  shared by all three clients. Logs: `/config/katzenqt/{a,b,c}.log` (client
  local time = UTC+2; host and replica logs are UTC).
- kpclientd container: `mixnet-alpine_da39a-kpclientd-1`, runs
  `/mixnet-alpine/kpclientd.alpine -c /mixnet-alpine/client/client.toml` on
  `127.0.0.1:64331`, epoch 2m.
- Relaunching the 3 GUI clients: quit them via VNC, then from a desktop
  terminal inside the container run `cd /config/katzenqt/webtop && make
  launch-3`. That target sets `KQT_STATE=a|b|c`,
  `KATZENQT_THINCLIENT_CONFIG=/config/katzenqt/webtop/thinclient-webtop.toml`
  and `UV_PROJECT_ENVIRONMENT=/config/.venv-katzenqt`, appending to
  `/config/katzenqt/{a,b,c}.log`. Launching by hand instead needs
  `DISPLAY=:0`, `WAYLAND_DISPLAY=wayland-0`, `XDG_RUNTIME_DIR=/config/.XDG`,
  `QT_QPA_PLATFORM=wayland`, `DBUS_SESSION_BUS_ADDRESS=...` and `-u abc`;
  without them the system tray is unavailable and `window.systray` stays
  `None`, which crashes the conversation-selected path (`'NoneType' object has
  no attribute 'has_read_messages'`).

---

## 1. katzenpost replica proxy-sweep storm (RESOLVED)

Root cause: the dead-substream read amplification removed by item 3 was the
dominant driver; the replica proxy machinery itself works to spec and is
bounded. Verified 2026-09-19 on the running 5-replica testnet (rev
`2609c423`, binary built 09-07; `replica/` at HEAD differs only in hpqc error
plumbing).

### Evidence

- `proxy sweep budget exhausted` per replica/day: peak ~40k (09-10/11), ~5-6k
  steady (09-12..17, before item 3 landed in the webtop clients), ~3.5k
  (09-18), and 19-207 today (09-19 half-day) —
  `/home/kpdev/katzenpost/docker/mixnet-alpine/replica{1..5}/katzenpost.log`.
  The logs are time-only; a day boundary is a backward timestamp jump > 8h,
  with the second startup marker as a known anchor.
- The replicas still sit at ~0.85 core each (`podman stats`), but that is the
  CTIDH1024 cost of the 3 webtop clients polling at the usual rate, not a
  storm: today ~42k local shard reads (100% miss) + ~21k proxied reads against
  ~0 writes. Apportioned to item 10.

### Disposition of the three suspects

1. No unbounded probe loop: the failover sweep is budget-bounded
   (`proxySweepBudget`/`proxyAttemptTimeout`, `handlers.go:617-667`), the read
   reply comes from the first authoritative holder
   (`proxyReadSweep`/`proxyReadRequest`, `handlers.go:802-941`), read-repair is
   double-gated (`handlers.go:920-924`), and dead-peer waiters are released
   immediately (`FailPeer`/`FailRequest`, `proxy_request_manager.go:100-139`).
2. Ack-before-durability is by design, not a bug: the courier ACK is
   explicitly "accepted, not durable" (`ackReply`,
   `courier/server/plugin.go`), replica dispatch is async and bounded by the
   in-flight collapse + `dispatchSem` (`scheduleReplicaDispatch`), and
   `CacheReply` overwrites transient `err=1`/`err=9` (BoxIDNotFound=1,
   ReplicationFailed=9, `pigeonhole/errors.go`) as writes propagate.
3. `ProxyRequestTimeout = 0` / `ProxyWorkerCount = 0` in `replica.toml` are
   sane: the startup log prints `Replica runtime defaults:
   ProxyWorkerCount=6, IncomingQueueSize=192, ProxyRequestTimeout=30s`
   (config.ApplyRuntimeDefaults).

---

## 2. `:substream:` synthetic peers leaked into the GUI user list (DONE)

Synthetic `:substream:*` peers are filtered out of the contacts tree and out of
the voucher who-reply, and a peer *name* can no longer spoof a substream.
`d8f943d` (filter), `0510097` (tests), `a3d2e2bc44b` (spoof guard, merged via
`27fbe52`).

---

## 3. Dead-substream read amplification (DONE)

A substream read that hits its first `BoxIDNotFound`/`Tombstone` now fails fast
— cancel the in-flight ARQ and drain task, delete the is_read MixWAL row,
deactivate the peer — so dead substreams stopped re-polling forever and feeding
the replica proxy storm, and a failed arming pass can no longer kill the
session's only read-arming task. `3bba25a`, `169af71`.

Schema facts worth keeping (re-derivable from `persistent.py`, easy to get
wrong):

- `mixwal.bacap_stream` is stored as 32 hex chars, no dashes.
- `current_message_index` / `next_index` are 104-byte blobs whose first 8 bytes
  are the little-endian uint64 Pigeonhole box index (BACAP counters).
- A substream's write/read caps start at their own index 0, independently of
  the parent stream.
- A retired substream peer (`active=0`, ReceivedPiece rows pruned, no MixWAL
  row) is the **normal terminal state** after F-assembly, not a stall. The
  fail-fast deactivate above looks similar but WARNING-logs and keeps the
  pieces.

---

## 4. Substream download status in the GUI (DONE)

Substream downloads appear in a Transfers panel (contact / pieces-of-total /
state) fed by `network.substream_progress_queue`, with the denominator carried
in the extended 140-byte I-chunk and persisted as
`ReadCapWAL.substream_total_chunks`. `346ba4f`, `11cd56c`, `b80e715`,
`08a253b`, migration `c4f1a8b2e9d7`.

Cancel was deliberately deferred; item 9 should decide whether upload cancel
and download cancel share machinery.

---

## 5. Per-peer pause/resume (DONE)

`pause_peer_reads` / `resume_peer_reads` freeze and re-arm a single BACAP
stream from its saved `next_index`, surfaced as a right-click action on
contacts-tree peer rows. `3bba25a`.

---

## 6. Modal dialogs inside QtAsyncio tasks corrupted tasks (DONE)

Blocking modals (`dialog.exec()`, `QInputDialog.getText`, `QMessageBox.*`,
`QMenu.exec`) run inside `@async_cb` tasks spun nested Qt event loops that
re-entered another task's `_step` and corrupted it — freezing the UI listeners
and silently eating an image send; every such site is now a sync slot, deferred
via `QTimer.singleShot`, or non-blocking through `_dialog_finished` /
`_menu_chosen`, the long-lived listeners are supervised and restarted,
`first_unread` persists on the io loop, and `async_cb` logs failures instead of
vanishing. `fdbde7d`, `bfe2a29`, `78e08d8`.

**Invariant to preserve:** never spin a nested Qt event loop inside a QtAsyncio
task. Blocking dialogs and menus are safe only from plain sync slots (top-level
event dispatch, no task mid-step) or from `QTimer.singleShot`; anything that
must happen inside a task goes through `_dialog_finished(dialog)` or
`_menu_chosen(menu, global_pos)`.

Verified on the 2026-09-18 3-party webtop rerun: no new re-entrancy tracebacks,
and the previously eaten image send completed end to end — bob's stuck
`conversationlog` row drained `network_status` 1 -> 2 once the relaunched
client armed the pending write, and alice and carol each assembled the
37300-byte `jamiroquai.webp` with md5 `00c8541c15e56ff317c15e755a97427e`,
byte-identical to the source. The `78e08d8` context-menu half was committed
with its rerun verification still in progress.

---

## 7. Tracked consideration: upgrade PySide6

We pin `pyside6~=6.9.3` (pyproject.toml:15). Item 6 established that
`PySide6.QtAsyncio` task stepping (QtAsyncio/tasks.py `_step`, the
`asyncio._enter_task` bookkeeping check) is intolerant of any re-entrant step,
e.g. a nested Qt event loop opened while a `QtTask` is mid-step; the mismatch
is raised as a `RuntimeError` and the reinvoked task is left in a corrupt
state.

We fixed the trigger at its source (item 6: never run a modal dialog inside a
task), so an upgrade is **not** needed to unblock that work. Tracked anyway for
later: a newer PySide6 (6.9.x point release or 6.10+) may harden `QtAsyncio`
itself — either by raising a clearer error for nested loop entry or by
tolerating it. Before upgrading, verify:

- Which `PySide6.QtAsyncio` changes landed since 6.9.3 (changelog /
  upstream issues about `_enter_task` / nested event loops).
- That the webtop GUI clients (Qt 6.9 ABI, Wayland) still run cleanly after
  the version bump, since the pin also covers runtime, not just bindings.
- That the item-6 manual rerun stays green on the new version (regression
  guard, not a substitute for fixing our own code).

Low priority; re-evaluate when we do the next dependency refresh.

---

## 8. Context-menu Pause/Resume enablement was asymmetric (DONE)

Both context menus disabled only Resume, leaving Pause clickable on an
already-paused row where the handler's state guard then swallowed the click as
a silent no-op; each now sets `pgm.setEnabled(active)` alongside
`rgm.setEnabled(not active)`. `455b7ad`.

---

## 9. Transfers panel entries for uploads, pausable and cancelable

The Transfers panel is receive-only today (`DownloadsModel` seeds from
substream `ConversationPeer` rows). A large outgoing file is invisible until
its chat bubble flips to sent, and it can be neither paused nor cancelled.
Uploads should get their own rows, pausable and cancelable.

### How an upload is wired today

- `SendOperation.serialize()` (`models.py:129-187`) splits an oversized message
  into C-chunks plus a final F-chunk on a fresh `agg_bacap_stream`, creates the
  indirection `ReadCapWAL(active=False, substream_total_chunks=C+1)`, and
  appends the I-chunk PlaintextWAL on the **main** conversation stream with
  `after_stream=agg_bacap_stream` and `indirection=rcw.id`.
- `_enqueue_outgoing_gcm` (`katzen.py:1147-1178`) persists those rows plus the
  optimistic `conversationlog` bubble with `network_status=1` and
  `outgoing_pwal=<I-chunk PWAL id>`.
- The writer sweep `send_resumable_plaintexts` (`network.py:1660-1732`) asks
  `PlaintextWAL.find_resendable()` (`persistent.py:730-790`) for dispatchable
  rows and starts one `start_resending` task per stream, guarded by
  `__resend_queue` and by "one msg per BACAP stream at a time".

### Pausing must not block the conversation

Requirement: while an upload is paused the user can keep sending chat
messages, which means the I-chunk that announces the substream lands at a
*later* main-stream box index than it would have.

This already works with the current gate semantics: `find_resendable` applies
the `after_id` / `after_stream` conditions **inside** the
`row_number() OVER (PARTITION BY bacap_stream)` window, so a gated-shut
I-chunk is excluded from the window entirely and a later eligible row on the
same stream gets `rownum=1` and dispatches. Worth verifying while implementing:
that `row_number()` has **no `ORDER BY`**, so per-stream pick order currently
relies on scan order.

Consequence to decide: local vs wire render order. The outgoing bubble's
`conversation_order` is fixed at send time, so the sender sees the image
*before* messages sent during the pause, while recipients render it in
arrival order, i.e. after. Either re-order the local row on resume or accept
the divergence deliberately.

### Pause mechanics (mirror `pause_peer_reads`, `network.py:1210-1254`)

- Stop the sweep from re-picking the substream's C/F rows: a persisted pause
  marker (nullable column on PlaintextWAL, needing an Alembic migration —
  precedent `c4f1a8b2e9d7`) or a persisted paused-stream set.
- Cancel the in-flight write task and its ARQ, delete pending `is_read=0`
  MixWAL rows for `agg_bacap_stream` so the drain sweep cannot re-cast them,
  and discard the stream from `__resend_queue`.
- **Blocker:** there is no write-side analogue of the `_inflight_reads`
  registry (`network.py:61`). Write tasks are tracked only loosely via
  `on_error(t, ...)` (`network.py:1732`) and the `draining_right_now` set, so
  a per-stream write-task registry is needed before a pause can cancel
  reliably.
- Resume: clear the marker and poke `resendable_event` / `__mixwal_updated`.

### Cancel mechanics

- Cancel is clean only while the I-chunk PlaintextWAL still exists (i.e. has
  not reached SentLog). Then delete: the substream C/F PlaintextWAL rows, the
  I-chunk PlaintextWAL, the indirection ReadCapWAL, the `agg_bacap_stream`
  WriteCapWAL, that stream's MixWAL rows, **and** the optimistic
  `conversationlog` row — the last is mandatory because its `outgoing_pwal`
  foreign key points at the I-chunk (`katzen.py:1176`).
- Boxes already ACK'd at couriers/replicas are orphaned but harmless: without
  the I-chunk nobody ever learns the substream read cap, so they are
  unreachable and expire.
- Once the I-chunk *has* been sent, recipients may already be downloading, so
  cancel must either be refused or degrade to "remove the local copy only".
  Decide which.

### GUI surface

- `DownloadsModel` (`qt_models.py:78+`) becomes direction-aware: a direction
  role/column distinguishing upload from download rows, and Pause/Resume/Cancel
  menu entries that apply per direction (item 8's enablement fix applies here
  too).
- `substream_progress_queue` (`network.py:92`) gains write-side events
  (upload started / chunk-acked / completed / paused / resumed / cancelled);
  today all its push sites are receive-side.
- Upload progress numerator: `substream_total_chunks - COUNT(PlaintextWAL
  WHERE bacap_stream = agg_bacap_stream)`, since PlaintextWAL rows are deleted
  as each chunk is ACK'd. The denominator is already persisted on the sender's
  indirection ReadCapWAL.
- Row key: the indirection `ReadCapWAL.id`, which mirrors the download side
  and is known before dispatch.
- Startup seeding: uploads have no `ConversationPeer` row, so seed from
  PlaintextWAL rows with `indirection IS NOT NULL` (an in-flight upload's
  I-chunk).

---

## 10. Client read-poll cadence keeps the replicas at the CTIDH ceiling

The 3 webtop GUI clients poll their streams at a fixed cadence and, with the
item-1 storm gone, the 5 replicas now sit at ~0.85 core each on that polling
cost alone. Today's replica traffic: ~42k local shard reads (100% miss) +
~21k proxied reads against ~0 writes — every one pays a CTIDH1024 op.

### How the cadence is wired

- The client re-polls a read cap every ~5s: `await asyncio.sleep(5)` in
  `drain_mixwal_read_single` (`network.py:835`), casting
  `no_retry_on_box_id_not_found=True` (`network.py:863`) so the daemon adds no
  ARQ noise on misses.
- Reads land on a random replica ~60%+ of the time and are proxied to the
  shard holder (`PROXY_REQUEST`, replica3 ≈ 9-23k/day), each costing another
  2-4 CTIDH ops.

### Question

Can the poll cadence relax (longer backoff on repeat misses, or notification
instead of polling) without hurting delivery latency, and can reads be routed
to a shard holder directly to skip the proxy hop? Pure perf/cost; not a
correctness bug. A different Sphinx/CTIDH geometry for the testnet is the
other lever. Evaluate when the item-9 transfer work next touches the read
path.

---

## 11. Per-epoch authority handshake churn (cosmetic noise)

Every ~120s (once per epoch) each servicenode courier's voting client fails
its first authority handshake, then retries cleanly:

```
WARN pki/voting/client/connector: authority authN: attempt 1 failed: peer authN
... handshake failed at message_4_receive ... use of closed network connection
```

Uniform across `servicenode{1,2,3}` (~6000 occurrences each since 09-07 in
`courier/courier.log`); the retry succeeds and `onGetConsensus` stays healthy
on the authority side, so it is noise, not an outage. Suspect a connection
teardown race at the epoch boundary in `pki/voting/client/connector`. Worth a
look the next time that code is touched.
