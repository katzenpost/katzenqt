# katzenqt / katzenpost TODO

## Maintenance rules for this file

This file is a living document and is committed to git (branch `deckard-dev`).
Keep it up to date as part of the working session:

- Items are written as GitHub-style checkboxes: `- [ ]` for open, `- [x]` for
  done. A short checkbox line summarizes the item; the prose below it carries
  the detail.
- Whenever a TODO item is completed, check it off (`- [x]`) or move it to a
  "Done" / "Resolved" section, and **commit** the change.
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

- [ ] Upgrade PySide6 once a newer release hardens `QtAsyncio` against nested
      loop entry. Low priority; re-evaluate at the next dependency refresh.

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

- [x] Give uploads their own Transfers-panel rows, pausable and cancelable.

Done. Phase 1 (display) landed in `f901bf8` ("transfers: show in-progress
uploads in the Transfers panel"); Phase 2 (pause/resume) in `42c0125`
("transfers: pause and resume in-progress uploads"); Phase 3's ordering
prerequisite in `b24b9fb` ("conversation log: order from MAX+1 and render by
actual order") and cancel in `47d90ad` ("transfers: cancel in-progress
uploads"). The per-phase detail below is retained for reference.

The Transfers panel is receive-only today (`DownloadsModel` seeds from
substream `ConversationPeer` rows). An in-progress upload has no row of its
own; the chat bubble is already visible (pending) but the only way to stop an
upload is to wait it out.

### How an upload is wired (corrected)

- `SendOperation.serialize()` (`models.py:131-221`) splits an oversized message
  into C-chunks plus a final F-chunk on a fresh `agg_bacap_stream`. The main
  stream gets a **gated I-chunk**
  `PlaintextWAL(after_stream=agg_bacap_stream, indirection=rcw.id)` and an
  indirection `ReadCapWAL(substream_total_chunks=C+1,
  write_cap_id=agg_bacap_stream)`. **All of these rows are committed at send
  time**, together with the optimistic `ConversationLog` bubble
  (`network_status=1`, `outgoing_pwal=<I-chunk id>`) by
  `persistent.append_outbound_chat` (`persistent.py:107-144`).
- The I-chunk is **not deferred-inserted**; it is *dispatched* only after the
  substream drains. `PlaintextWAL.find_resendable` (`persistent.py:754-829`)
  keeps the `after_stream` gate shut while any `PlaintextWAL` remains on
  `agg_bacap_stream` (`persistent.py:802-808`), and the I-chunk's main-stream
  BACAP box index is assigned at dispatch in `start_resending` ->
  `encrypt_write` (`network.py:1876-1895`).
- Ordering consequence (upload-general, not pause-specific): texts sent during
  an upload land on the main stream before the I-chunk (the main stream is
  otherwise idle and the gated I-chunk is excluded from the `row_number()`
  window), so **recipients render the image after those texts**, while the
  sender's optimistic row keeps its send-time `conversation_order` and never
  moves.
- Completion: the Transfers row disappears when the substream upload finishes,
  i.e. when the last C/F chunk is ACK'd (`remaining agg PlaintextWAL == 0`).
  The chat bubble stays pending until the I-chunk is *separately* ACK'd
  (`SentLog._ensure_sent_log_and_flip_status`, `persistent.py:639-660`). There
  is therefore no cancel-after-I-chunk case: once the I-chunk can dispatch the
  upload is already complete.

### Phase 1 — display in-progress uploads (done, `f901bf8`)

- Row key: the indirection `ReadCapWAL.id`, known at send time.
- Numerator: `substream_total_chunks - COUNT(PlaintextWAL WHERE bacap_stream =
  agg_bacap_stream)` (PWAL rows are deleted per ACK). Denominator:
  `ReadCapWAL.substream_total_chunks`.
- New `substream_progress_queue` kinds, kept distinct from the download kinds
  so existing events/tests are untouched: `upload_started` (rcw_id, conv_id,
  total, conversation name), `upload_piece` (rcw_id, sent), `upload_completed`
  (rcw_id), `upload_paused`, `upload_resumed`, `upload_cancelled`.
- `upload_started`: `persistent.append_outbound_chat` detects the I-chunk in
  `db_entries` and returns an upload descriptor (conversation name as parent);
  `network.notify_outbound_chat_sent` (`network.py:83-104`) pushes it
  post-commit.
- `upload_piece` / `upload_completed`: `SentLog.mark_sent` resolves `ReadCapWAL`
  by `write_cap_id == mw.bacap_stream` (non-null total) and counts remaining agg
  PWALs after the deleting transaction; `drain_mixwal_write_single`
  (`network.py:377-411`) pushes the event.
- `DownloadsModel` (`qt_models.py:85+`) becomes direction-aware: a `direction`
  field plus `ROLE_TRANSFER_DIRECTION`, State text `"Uploading"` /
  `"Downloading"`; `start_transfer(..., direction="download")` default keeps
  existing tests green.
- `transfers_listener` (`katzen.py:1695-1732`) routes the new kinds.
- `seed_from_db` (`qt_models.py:236-275`) additionally seeds `PlaintextWAL` rows
  with `indirection IS NOT NULL` where remaining agg PWALs > 0 (remaining==0
  means the substream already finished, so no row), direction upload, parent
  name = conversation name; paused state from the Phase-2 marker.
- Fix the latent `_order` bug at `katzen.py:1664-1672` (`_order` holds
  `uuid.UUID` but is compared to `str(rcw_id)`, so failed rows can never be
  dismissed).

### Phase 2 — pause (done, `42c0125`)

- Persist a per-stream marker: `WriteCapWAL.paused: bool` (one row per stream,
  mirrors receive-side `ConversationPeer.active`). New Alembic migration
  `b7d2e4f19a3c` with `down_revision='c4f1a8b2e9d7'` (the chain head,
  `add_substream_total_chunks_to_readcapwal`).
- `find_resendable` also excludes streams whose `WriteCapWAL.paused` is True.
- Add `_inflight_writes: dict[uuid.UUID, asyncio.Task]` (the missing write-side
  analogue of `_inflight_reads`, `network.py:69`); register where `write_task`
  is created (`network.py:1582`), pop in `_on_write_done`.
- `pause_upload(rcw_id)`: resolve the agg stream via `_upload_stream_for_rcw`
  (non-null `substream_total_chunks`); set the marker; cancel the in-flight
  write task; delete pending `is_read=False` MixWAL rows for the stream;
  discard `__resend_queue` (`draining_right_now` is cleared by the
  done-callback); poke `resendable_event` / `__mixwal_updated`; fire
  `upload_paused`.
- `resume_upload`: clear the marker, poke, fire `upload_resumed`.
- `transfers_context_menu` (`katzen.py:1639-1693`) branches on direction;
  Pause/Resume enabled per that row's own direction and state (mirror
  `pause_peer_reads` / `resume_peer_reads`, `network.py:1309-1376`).

### Phase 3 — cancel (done, `47d90ad`; ordering prerequisite `b24b9fb`)

Ordering prerequisite done in `b24b9fb`:
- `persistent.next_conversation_order` -> `coalesce(func.max(conversation_order),
  -1) + 1` (keep the per-conversation lock).
- `ConversationLogModel` gap-tolerant: caches the ordered
  `conversation_order` values and maps `index.row()` -> actual order in
  `data()` / `index()`; inserts the tail on unchanged-prefix growth, resets on
  any other change. The `index_row == conversation_order` assumption is gone.

`cancel_upload(rcw_id)` landed in `47d90ad`. Cancel exists only while the
upload is live (remaining agg PWAL > 0 and the I-chunk `PlaintextWAL.indirection
== rcw_id` still present); if the substream has completed, refuse. It cancels
the in-flight write, then in one transaction deletes the agg C/F `PlaintextWAL`
rows, the I-chunk `PlaintextWAL`, the indirection `ReadCapWAL`, the
`agg_bacap_stream` `WriteCapWAL`, that stream's MixWAL rows, and the optimistic
`ConversationLog` row (its `outgoing_pwal` FK forces this); discards
`__resend_queue`; fires `upload_cancelled` and wakes
`conversation_update_queue`.

### Tests

- `tests/test_downloads_model.py`: direction role, `"Uploading"` state, upload
  seeding (completed uploads skipped).
- `tests/test_network_fake.py`: `upload_started` / `upload_piece` /
  `upload_completed` events; pause/resume/cancel.
- Ordering: delete-middle-row then append uses old MAX+1 with no unique
  violation; `test_concurrent_write_orders.py:90` still passes.

---

## 10. Client read-poll cadence keeps the replicas at the CTIDH ceiling

- [ ] Evaluate relaxing the client read-poll cadence and/or routing reads
      directly to a shard holder to cut the replica CTIDH load.

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

- [ ] Look at the epoch-boundary handshake teardown race the next time that
      code is touched.

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

---

# Old TODO backlog

The following is the entire contents of `TODO.md` as it stood on the
`deckard-todo` branch, retained verbatim at merge time. Most of it is
probably-obsolete backlog that has not been triaged against current `main`;
do not investigate it now. It is kept as a quarry of prior findings and
follow-ups that may still be relevant.

## Active backlog (deckard-todo)

## `conversation_order` / message deletion

- [x] **ConversationLog: assign `conversation_order` from `MAX(order)+1` instead of
      `COUNT(*)`, so message deletion doesn't corrupt ordering.**
      Done in `b24b9fb` ("conversation log: order from MAX+1 and render by
      actual order"): `persistent.next_conversation_order()` now returns
      `coalesce(func.max(conversation_order), -1) + 1`, and
      `ConversationLogModel` caches the ordered `conversation_order` values and
      maps `index.row()` to the actual order, so rows past a deletion gap stay
      addressable. `test_voucher_guard.py`'s `_append_log_row_async` now routes
      through the shared helper. The original item text follows.
      `persistent.next_conversation_order()` (`persistent.py:94`) returned a live
      `select(count())` scalar subquery; every production append site uses it under the
      per-conversation `conversation_log_order_lock`: `append_outbound_chat`
      (`persistent.py:139`), `ConversationLog.append_from` (`persistent.py:954`), the
      voucher induction (`voucher.py:589`), and local tally rows
      (`_stage_local_tally`, `katzen.py:2455`). The create-conversation row seeds
      order 0 directly (`katzen.py:2110`, `headless/_actions.py:162`).
      COUNT under-counts as soon as any log row is deleted: the next append reuses the
      highest surviving order and trips `UniqueConstraint('conversation_id',
      'conversation_order')` (`persistent.py:907`), dropping the message. Replace the
      helper's body with `coalesce(func.max(ConversationLog.conversation_order), -1) + 1`.
      It is behavior-identical while nothing is deleted (after the order-0 create row,
      `COUNT == MAX+1`). Keep the lock: MAX+1 removes the delete-vs-append hazard, not
      append-vs-append.
      The model renders rows by index: `data()` resolves a row via
      `conversation_order == index_row` (`qt_models.py:741`) and `index()` bounds by
      `_row_count` (`qt_models.py:604`), and `refresh_row_count()`'s shrink→reset path
      assumes a contiguous `0..count-1` (`qt_models.py:639`). Once deletion leaves gaps,
      row indices no longer match orders and rows past a gap become unreachable/None;
      deletion and rendering must enumerate by actual `conversation_order`, not row
      index. Unread space is `>=`-compared (`first_unread`, `new_poll_count`,
      `tally/presenter.py:338,367`), so gaps are tolerable there.
      Tests: `test_voucher_guard.py:211` (`_append_log_row_async`) hand-rolls the COUNT
      subquery — route it through the helper; add a delete-a-middle-row then append case
      asserting the new order is the old MAX+1 with no unique violation;
      `test_concurrent_write_orders.py:90` (`orders == range(n)`) must still pass.

- [ ] **(Deferred; needs migration) Make `conversation_order` assignment atomic and
      app-lock-free via a per-conversation counter table.**
      Order assignment relies on every append site funnelling through the single io-loop
      writer and the intra-process `conversation_log_order_lock` (`persistent.py:31-47`,
      `katzen.py:1394`). That is correct in one process but does nothing if a second
      process appends the same conversation (a future GUI + headless worker on one state
      file). The clean design is one counter row per conversation and, inside the append
      transaction, `UPDATE counter SET next = next + 1 WHERE conversation_id = ?
      RETURNING next`, then INSERT ConversationLog with that literal value. SQLite
      serializes the UPDATE, so it is correct in-process and cross-process with no app
      lock and no funnel dependency, and it composes with the MAX+1 item above. Cost: a
      new table = a migration, deliberately avoided (an upgrade with a migration can't be
      rolled back by users). Do only if the single-process assumption breaks or a
      forgotten-lock bug bites.

- [ ] **Delete a single chat message from the right-click context menu.**
      Add a context menu on a chat row in `resources/chatview.qml` (mirror the
      context-menu pattern used for the transfers/contacts trees) that calls a
      `MainWindow` handler with the row's `message_id` role (`ROLE_CHAT_MESSAGE_ID`,
      `qt_models.py:755`), and delete the matching `ConversationLog` row.
      Depends on the ordering work above: deletion leaves a gap, so without MAX+1 the
      next append collides and without order-based rendering the view mis-maps rows. The
      shrink→reset path (`qt_models.py:659`) already reconciles the count and clears the
      per-index caches, so the handler must commit and then wake
      `conversation_update_queue`.
      Implementation notes:
        - Delete on the io loop (async engine, `iothread.run_in_io`) inside
          `conversation_log_order_lock(conversation_id)`; never on the Qt thread / sync
          engine mid-append.
        - Scope is local-only removal by default; there is no delete protocol on the
          mixnet, so a tombstone or peer propagation would need one.
        - Tally events are ordinary ConversationLog rows, so deleting one does not
          retract the survey from `TallyState` or from peers; decide whether to allow
          deleting any row or restrict to normal chat/file rows.
        - Reconcile related state: `outgoing_pwal`/PlaintextWAL, SentLog, attachments,
          and `Conversation.first_unread` when the deleted row is the unread pointer.
        - Decide confirmation/undo. A local delete survives a later daemon replay: rows
          are consumed by read-cap index, so a re-fetch does not recreate an old row.

## Old TODO backlog (deckard-todo)

What follows is mostly out of date but still being retained for now pending
furher review.

## deckard-wip follow-ups

- [ ] **Cross-repo lockstep sync (thin_client-changes.txt, FETCH_NOTES.md).**
      katzenpost `docker/thin_client-changes.txt` and `docker/FETCH_NOTES.md`
      list the pin-sync rules across katzenpost/thin_client/katzenqt; add a
      note describing the 0.0.24 changes (unconditional replay on every
      reconnect; TCP keepalive + `TCP_USER_TIMEOUT` on the daemon socket).
      Also revisit the lockstep refs that still reference the old pair:
      `.github/workflows/test-integration-docker.yml` pins katzenpost
      `d5a6349a` ("lockstep with thin_client 0.0.23 CI") and katzenpost
      docker Makefile `thin_client_ref?=`.

## Future work (carried over from prior fix branches)

- [ ] **Daemon read ride-out epoch staleness (separate bug).** The queued-read
      fallback in `_read_box` goes stale across PKI epochs:
      `src/katzenqt/voucher.py:147` has `TODO(workaround) — the daemon read
      ride-out goes stale across PKI epochs`, and `DRAFT.md` tracks it as "Join
      stall under long waits". Symptom: after an epoch rollover, a pending
      read/join can stall instead of riding out. Needs its own repro and fix,
      independent of the thinclient reconnect work.

## kpclientd port discovery (dependencies)

- [ ] **Auto-discover the docker mixnet's kpclientd port.** Until recently the
      kpclientd port was static (64331); it became dynamic-per-checkout so
      multiple mixnets can run from different checkouts at once. katzenqt and
      the thin_client integration tests still hardcode/parameterize it:
      `tests/integration/conftest.py` defaults to `64331` with a
      `KATZENQT_KPCLIENTD_PORT` override, and the GUI's
      `src/katzenqt/data/thinclient.toml` is hand-edited locally
      (Address `host_localhost:44977`, left uncommitted). Teach katzenqt and
      the thin_client integration tests to find the port automatically (e.g. by
      inspecting the docker compose / container rather than hand-editing the
      config or exporting an env var), and retire the local `thinclient.toml`
      edit. Exact mechanism TBD — do not start until the previous items land.
