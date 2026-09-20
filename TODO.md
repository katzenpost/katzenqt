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

---

## 7. Tracked consideration: upgrade PySide6

We pin `pyside6~=6.9.3` (pyproject.toml:15). The no-nested-Qt-loop rule (see
the re-entrancy section in `AGENTS.md`) rests on the fact that
`PySide6.QtAsyncio` task stepping (QtAsyncio/tasks.py `_step`, the
`asyncio._enter_task` bookkeeping check) is intolerant of any re-entrant step,
e.g. a nested Qt event loop opened while a `QtTask` is mid-step; the mismatch
is raised as a `RuntimeError` and the reinvoked task is left in a corrupt
state.

Client code obeys that rule, so an upgrade is **not** needed to unblock that
work. Tracked anyway for later: a newer PySide6 (6.9.x point release or 6.10+)
may harden `QtAsyncio` itself — either by raising a clearer error for nested
loop entry or by tolerating it. Before upgrading, verify:

- Which `PySide6.QtAsyncio` changes landed since 6.9.3 (changelog /
  upstream issues about `_enter_task` / nested event loops).
- That the webtop GUI clients (Qt 6.9 ABI, Wayland) still run cleanly after
  the version bump, since the pin also covers runtime, not just bindings.
- That a manual rerun of the modal-dialog flows stays green on the new version
  (regression guard, not a substitute for the no-nested-loop rule).

Low priority; re-evaluate when we do the next dependency refresh.

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
  menu entries that apply per direction (each entry's enablement must follow
  its own direction and state).
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
dead-substream read amplification gone, the 5 replicas now sit at ~0.85 core
each on that polling cost alone. Today's replica traffic: ~42k local shard
reads (100% miss) + ~21k proxied reads against ~0 writes — every one pays a
CTIDH1024 op.

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
