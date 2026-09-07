# TODO: remaining review-findings fixes for katzenqt

Context for a fresh session. REVIEW.md (repo root) holds the review; REVIEW2.md
(repo root) maps every item to the commit(s) on
`deckard-wip..review-findings-fixes` that address it, or to a TODO. This file
lists the items that remain **TODO** in REVIEW2.md, plus the one extra item the
maintainer opted into (48-5 funneling).

Nothing here is implemented yet. All line numbers below are accurate against
`review-review-findings-fixes` (= PR #56, out of scope for this work) as of this
writing.

## Baseline

- Repo root: `/home/human/deckard/katzenqt`. Git remote `origin` =
  github.com/katzenpost/katzenqt.
- PR/branch numbering (what the REVIEW.md text calls "#46" is actually PR #49):
  - #45 `3party` (= `origin/pr45`)
  - #49 `fix-conversation-log-lock-deadlock` — the review's "46" (45-… items)
  - #47 `fix-sqlite-write-wedge` (= `origin/pr47`)
  - #53 `misc-fixes` (= `origin/pr53`)
  - #48 `deckard-wip` (= `origin/pr48`, the current base / tip)
  - Stack order: 45 -> 49 -> 47 -> 53 -> 48. #46 was accidentally force-pushed
    out of history; #49 replaces it.
- `review-findings-fixes` = deckard-wip plus 14 fix commits (newest->oldest):
  b1698e5, 5f7f736, ea31ea2, cef0572, 56ce966, fdfe104, 2bf216f, b0185dc,
  4c2a4d9, 6bc6aa0, 6218ffe, ddf2d32, b2f3b0c, 2cad52d.
- Do NOT run tests inside `uv run pytest`: `uv run` re-syncs the venv and can
  revert manual installs. Use the `.venv/bin/python -m pytest` invocation in
  "Verification" below. `tests` is a namespace package (no `tests/__init__.py`);
  the "package-shadowing" pitfall (ModuleNotFoundError: No module named
  'tests.fakes' while loading tests/conftest.py) bites when the workspace tests
  package shadows an installed `tests` package. `uv run ruff`/`mypy` are fine.
- Qt tests need `QT_QPA_PLATFORM=offscreen QT_QUICK_BACKEND=software`.
- No Alembic migration and no PySide6 `.ui` regeneration is needed for any of
  these changes (no schema / UI-file change).

---

## Item 1 — 45-4b: "already inducted" guard (HIGH importance, correctness)

**done in 0b4f3b5.**

**Motivation.** `voucher._add_peer` inserts a fresh `ReadCapWAL` +
`ConversationPeer` unconditionally. A second run of the induction path for the
*same* voucher (a retried/double GUI induction, or a handshake the wire let
through twice) re-adds the joiner on the same salt-mutated read cap, producing a
duplicate member: the same stream read twice, duplicate UI rows, a doubled entry
in who-replies, and an over-large `conversation.peers` for every later member.
The daemon may tolerate re-consumed vouchers, so this is a reachable, real
corruption rather than a theoretical one.

**Current code (anchors).**
- `voucher.py:243` `_add_peer(sess, conversation, name, read_cap)` — sync,
  no dedup; malformed-cap guard only (`len != 136`), then
  `sess.add(ReadCapWAL(...))` (:255) + `sess.add(ConversationPeer(...))` (:259).
- `voucher.py:465` `derive_read_and_induct` (inductor side): `voucher_induct`
  at :496; session block at :503-508 does `_add_peer(sess, conv, joiner_name,
  induct.mutated_message_read_cap)` (:505), deletes the PendingVoucher row
  (:506-507), commits; then always fires `send_introduction_message` (:510-512).
- `voucher.py:320` `await_and_open` (joiner side): the `please_adds` loop at
  :367-368 calls `_add_peer` per entry.
- `conversation_handlers.py:73-76` `_handle_introduction` already guards with
  `_already_has` (:81-108) before `_add_peer`.
- `persistent.py` models: `ReadCapWAL` (:314), `ConversationPeer` (:683,
  `read_cap_id` FK), membership join via `ConversationPeerLink`.

**Required changes.**
1. Add to `persistent.py`:
   `async def peer_has_read_cap(sess, conversation_id: int, read_cap: bytes) -> bool`
   — explicit query `select(ReadCapWAL.read_cap)` joined
   ReadCapWAL -> ConversationPeer (`read_cap_id`) -> ConversationPeerLink
   (`conversation_id == conversation_id`), return `read_cap in caps`. It must be
   an explicit join query, NOT `conv.peers` relationship traversal: the callers
   run in SQLAlchemy's async session where touching a lazy relationship raises
   `MissingGreenlet` (same constraint documented at
   `conversation_handlers.py:91-93`).
2. Refactor `conversation_handlers._already_has` (conversation_handlers.py:81)
   to delegate to `peer_has_read_cap(sess, conv_id, intro.read_cap)`, keeping its
   docstring. Rationale: the review flags duplicated idioms; do not add a fourth
   copy of this join query.
3. In `derive_read_and_induct` (:503-508): if
   `await peer_has_read_cap(sess, conversation_id, induct.mutated_message_read_cap)`,
   log a WARNING "already inducted; skipping duplicate peer for <name>", and skip
   `_add_peer`. In ALL cases (duplicate or not) still delete the PendingVoucher
   row and commit, and still call `send_introduction_message`. **Decision (baked
   in): the INTRODUCTION is ALWAYS resent** on the duplicate path — it resumes a
   possibly-lost announcement; members dedup it via `_already_has`.
4. In `await_and_open` (:367-368): guard each `_add_peer` with
   `peer_has_read_cap`; skip + log WARNING if present (defensive; who-reply caps
   are normally distinct).

**Tests.**
- `tests/test_voucher_helpers.py` or `tests/test_voucher_guard.py`:
  - `peer_has_read_cap` true/false across: present peer, absent cap, different
    conversation, and the own-peer case. `_make_conversation`/`_add_active_peer`
    helpers already exist in `tests/test_voucher_guard.py`.
- `derive_read_and_induct` idempotency: run it twice through a stub connection
  (a tiny async class with `voucher_derive_stream` + `voucher_induct` returning
  canned `mutated_message_read_cap`/`display_name`), with
  `monkeypatch`ped `voucher._read_box`, `voucher._publish_box`,
  `voucher._build_who_reply`, and `voucher.send_introduction_message` (record
  call count). Assert: run 2 adds no new `ReadCapWAL`/`ConversationPeer` row,
  deletes the PendingVoucher, and `send_introduction_message` was called both
  times.
- Optional: same stub for `await_and_open`'s duplicate-skip.

---

## Item 2 — 48-2b: harden the two listener loops (MEDIUM-HIGH importance, availability)

**done in 3ab93e5.**

**Motivation.** `receive_msg_listener` and `peer_added_listener` are bare
`while True:` bodies. Any unexpected exception kills the listener until a full
restart, silently freezing UI refresh (new messages stop appearing, announced
members never show in the contact tree) — visible only in the logs. The known
root causes (kp_client AttributeError, missing conversation state, unbounded
wait) were fixed via ddf2d32; this is defense-in-depth so a future unknown bug
can't take the whole UI out.

**Current code (anchors).** `katzen.py:544` `receive_msg_listener`,
`katzen.py:588` `peer_added_listener`; started by `create_task` at
`katzen.py:1282-1283` (`katzen_util.create_task` already logs failures at ERROR,
which is why these loops dying is currently *only* a log line).

**Required changes.**
- Wrap each loop body in `try/except`:
  - `except asyncio.CancelledError: raise` (re-raise, NOT caught by
    `except Exception` — CancelledError is a BaseException in Python 3.8+).
  - `except Exception as e:` -> `logger.error("<listener>: dropping an item "
    "after %s", e, exc_info=e)` then `continue` (log-and-continue, **decision
    baked in**: no modal, no auto-restart of the loop).
  - Decision: wrap the whole body including the `await run_in_io(queue.get())`
    so the queue-get / wait-for-state / UI-refresh unit is covered; a torn-down
    loop still fails loudly.
- Optional but recommended for tidiness: extract the per-item bodies into
  `_process_conversation_update(conversation_id, redraw_only)` and
  `_process_peer_added(conversation_id, name)`. Do this only if it makes the
  try/except cleaner; the loops are QML-heavy so don't build elaborate stubs.

**Tests.** Light stub-based unit tests ONLY if the `_process_*` extraction is
done (stub `conversation_state_by_id`, `conversation_log_model`,
`qml_ChatLines`, `systray`, `QStandardItem`). Otherwise rely on the existing unit
suite + integration/manual coverage. Either way, the hardening itself is covered
by code review + the "listener still alive after an injected exception" idea if a
stub test is cheap.

---

## Item 3 — 47-R1: unify the three network done-callbacks (MEDIUM-LOW importance, maintainability)

**done in 9f78e29.**

**Motivation.** `network.py` has three near-identical bespoke asyncio
done-callbacks that have already drifted (exc_info logging added piecemeal;
cancellation handled differently: `on_error` ignores it, the drain handlers
release the stream on it). This is the review's "duplicated idiom" finding.
**Decision (baked in): keep the three existing names as thin wrappers over one
shared primitive; leave the public signatures unchanged so existing tests keep
compiling.**

**Current code (anchors).** `network.py` `_on_write_done` (:749-772),
`_on_read_done` (:832-850), `on_error` (:968-988), `on_error` used at :1053
(send_resendable_plaintexts). Semantics to preserve exactly:
- write drain: exception -> discard stream from `draining_right_now` + poke
  `__mixwal_updated` (wakes the writer); cancellation -> discard only.
- read drain: exception -> discard + set the readables event; cancellation ->
  discard only.
- `on_error`: exception -> call `func(*args, **kwargs)`; cancellation -> no-op;
  log at ERROR with `exc_info` (:985).
- The existing drain messages ("… crashed for bacap_stream=…; releasing stream")
  must keep their wording — pass them in as a `desc` so logs stay greppable.

**Required changes.**
1. Add one private primitive, e.g.
   `def _done_callback(task, *, desc, on_cancel=None, on_error=None) -> None`
   in `network.py`: on cancellation call `on_cancel()`; on exception log
   `logger.error(f"{desc}: %r", exc, exc_info=...)` and call `on_error(exc)`;
   NEVER re-raise (a done-callback raise only surfaces as asyncio "Exception in
   callback" spam — same rationale as `tests/test_katzen_util.py`).
2. Reimplement `_on_write_done` / `_on_read_done` / `on_error` as wrappers:
   same signatures and naming. Existing tests `tests/test_network_fake.py:492-495`
   call `network._on_write_done` directly — keep that working.
3. Do NOT fold in `katzen_util.create_task` (separate module, different
   contract — it both logs and lets the task's exception be re-raised on await;
   leave it alone).

**Tests.** Add to `tests/test_network_fake.py` a small direct test of the
primitive: exception -> on_error hook fired + ERROR logged with exc_info and no
"Exception in callback" (mirror the loop exception-handler pattern from
`tests/test_katzen_util.py:9-37`); cancellation -> on_cancel fired, no on_error,
no log; success -> neither hook. Existing `_on_write_done` and `on_error` tests
(the latter at `tests/test_index_invariants.py:276-293`) must stay green.

---

## Item 4 — 45-dup-note: duplication-count discrepancy (LOW importance, documentation only)

**done in e561d24.**

**Motivation.** REVIEW.md says there are "five duplication findings" but names
only three: `persistent.wait_for_sent`, `voucher.as_introduction`, and
`persistent.next_conversation_order`. REVIEW2.md flagged the count.

**Done — audit + dedupe (deviation from the original decision).** Instead of
closing as a review-text inconsistency only, an audit was run for the
"duplication REVIEW.md alluded to". Mapping of the "five" (three named +
two from the PR#47 reuse block): all are accounted for —
`next_conversation_order`, `wait_for_sent`, `as_introduction`,
`_ensure_sent_log_and_flip_status` (finalize block), and the
`_on_write_done`-reimplements-`on_error` reuse (now the `_done_callback`
primitive, item 3 / commit 9f78e29). The one remaining genuine cross-file
duplicate was the conversation owner's salt-mutated read-cap lookup, hand-written
in two different shapes in `_handle_introduction` and `_build_who_reply`; it is
now a single `persistent.own_read_cap` helper (commit e561d24).

---

## Item 5 — 48-5: funnel `new_conversation` through the io-loop writer (MEDIUM importance, threading correctness — ADDED on maintainer request)

**Motivation.** persistent.py's funnel comment (:31-47) claims every
ConversationLog-append site serialises through the io-loop single writer and
carves out `new_conversation` as "no funnel needed" on `_engine_sync`. The
finding: `new_conversation` still does a synchronous DB commit directly on the
Qt GUI thread — the exact cross-loop hazard the fix series exists to close, and
the carve-out makes the comment/behaviour inconsistent. The GUI send path
(`chat_msg_single_line`, katzen.py:479-521) already funnels through
`run_in_io(network.notify_outbound_chat_sent(...))`; `new_conversation` should
look the same.

**Current code (anchors).**
- `katzen.py:863-913` `new_conversation` (async_cb, Qt thread; pops two
  QInputDialog boxes, builds `wcapwal`/`rcapwal`/`convo`/`own_peer`/
  `first_post` ORM objects, commits via
  `with persistent.Session(persistent._engine_sync) as sess:` at :901-912 with
  refresh of each object, then `await add_conversation(self, convo)` at :913 on
  the Qt thread).
- `katzen.py:1176` `add_conversation(window, convo)` necessarily runs on the Qt
  thread (builds `QStandardItem`s, `ConversationUIState`, roster rows) and reads
  `convo.id`, `convo.own_peer_id`, `convo.own_peer.name`, `convo.write_cap`,
  `convo.first_unread`, `convo.peers`. All of these are in-memory/loaded attrs
  (backref-loaded `own_peer`), so a refreshed-detached instance is fine.
- `katzen.py:84-94` `AsyncioThread.run_in_io(fn)` awaits `fn` on the io loop and
  returns its result (`asyncio.run_coroutine_threadsafe` + wrap_future).
- `katzen.py:681-686` `send_file` has the **identical** `_engine_sync` commit
  block on the Qt thread (creates WriteCapWAL + PlaintextWAL/ReadCapWal rows for
  a SendOperation). The feature is mostly a TODO stub ("TODO actually send
  them"), but the block is real. **Recommended follow-up:** route it through the
  same funnel helper once it exists, for consistency. Flag to maintainer if you
  want to widen scope to include it in this change.

**Required changes.**
1. Add a module-level async coroutine in `katzen.py`, e.g.
   `async def _commit_new_conversation(wcapwal, rcapwal, convo, own_peer, first_post)`:
   ```
   async with persistent.asession() as sess:
       sess.add(wcapwal); sess.add(rcapwal); sess.add(convo)
       sess.add(own_peer); sess.add(first_post)
       await sess.commit()
       await sess.refresh(convo); await sess.refresh(own_peer)
       await sess.refresh(wcapwal); await sess.refresh(rcapwal)
       await sess.refresh(first_post)
   ```
   (Refresh in place so the SAME object instances — already attached to the
   caller's `convo` reference — carry the generated ids/FKs back to
   `add_conversation`.)
2. In `new_conversation`, replace the `with persistent.Session(_engine_sync)`
   block (:901-912) with
   `await self.iothread.run_in_io(_commit_new_conversation(wcapwal, rcapwal, convo, own_peer, first_post))`,
   keeping `await add_conversation(self, convo)` on the Qt thread.
3. Rewrite the comment at :895-900 to describe the io-loop funnel (mirror the
   wording of the `notify_outbound_chat_sent` comment at :502-510), and update
   `persistent.py:36-39` to remove the "no funnel" `new_conversation` carve-out.
4. Do NOT remove `_engine_sync`: `send_file` (katzen.py:681) and any
   headless/`_engine_sync` users still use it.
5. `first_post.conversation_order` is hardcoded 0 (no count subquery), so no
   `conversation_log_order_lock` is needed here — leave the lock usage alone.

**Tests.** `tests/test_models.py` or a new `tests/test_katzen_util.py`-style
minimal unit test for `_commit_new_conversation` (call it directly, assert the
rows exist and ids/FKs are populated) if cheap. End-to-end `new_conversation` is
manual (QInputDialog) — at minimum create a conversation on a running client and
confirm the row set, roster, and that the Qt thread is not blocked. Keep existing
`tests/test_qt_decoupling.py` and `tests/test_headless_module.py` green.

---

## Decision set (baked in)

1. 45-4b: duplicate-path guard still **resends the INTRODUCTION**.
2. 47-R1: keep `_on_write_done`/`_on_read_done`/`on_error` names as thin
   wrappers; do not touch `katzen_util.create_task`.
3. 48-2b: **log-and-continue**; no modal, no loop auto-restart.
4. 45-dup-note: close as review-text inconsistency (no audit).
5. 48-5: funnel `new_conversation` (in scope); `send_file` twin = recommended
   follow-up; 48-1 backstop (1200s flat backstop that still trips benignly after
   ~20min idle) stays as-is, out of scope.

## Verification

- `uv run ruff check src/katzenqt tests`
- `uv run mypy src/katzenqt`
- `.venv/bin/python -m pytest -o addopts="" -m "not integration" tests/test_voucher_helpers.py tests/test_voucher_guard.py tests/test_voucher_read_box.py tests/test_network.py tests/test_network_fake.py tests/test_conversation_handlers.py tests/test_katzen_util.py tests/test_index_invariants.py tests/test_qt_decoupling.py tests/test_models.py -q`
- Wrap any Qt-touching test with `QT_QPA_PLATFORM=offscreen` /
  `QT_QUICK_BACKEND=software`.

## Suggested order

Implement 1 -> 2 -> 3 -> 4 -> 5 (independent; keep the unit suite green after
each). Commit after each item is done, and then commit an update to this
TODO.md noting an item is done and which commit hash it was done in. This
document is the hand-off for implementation.
