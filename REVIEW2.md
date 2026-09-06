# REVIEW2.md — action items from REVIEW.md, mapped to `review-findings-fixes`

This file enumerates every actionable item in REVIEW.md and, for each, states
either which commit(s) on `deckard-wip..review-findings-fixes` address it or
what remains open as a TODO.

Ordering mirrors REVIEW.md: cross-stack notes first, then PR #45, #47, #48, #53.
Partially-addressed items are split into sub-items (a)/(b) so each state is
unambiguous.

PR-number note: the review was written when #46 had (transiently) merged into
#45 before that merge was force-pushed away. The branch REVIEW.md calls "#46"
is `fix-conversation-log-lock-deadlock`, which is now PR **#49**. The five-PR
stack as of the review is #45 (3party) → #49 (fix-conversation-log-lock-deadlock)
→ #47 (fix-sqlite-write-wedge) → #53 (misc-fixes) → #48 (deckard-wip). PR #56
(`review-review-findings-fixes`) is out of scope here.

---

## Cross-stack interactions

### 1. Read-drain task's safety net lacks `_on_write_done`-equivalent `exc_info` logging
**Addressed.** `_on_read_done` (which #48 already added) now logs the crashing
exception with `logger.error(..., exc_info=exc)` exactly as `_on_write_done`
does, and keeps the stream-release + `readables_to_mixwal_event.set()` wakeup.
`src/katzenqt/network.py:832` — commit `2cad52d`.

### 2. Write-path OSError asymmetry + zero-backoff retry
**Addressed.**
- (a) OSError included in `drain_mixwal_write_single`'s give-up list, matching
  the read path: `network.py:183`.
- (b) Both give-up paths now `await asyncio.sleep(5)` before releasing the
  stream, so a persistent failure backs off instead of hot-looping (previously
  zero backoff on the write path): `network.py:190`, `network.py:514`.

`2cad52d`.

### 3. Dropping `pool_size=1000` compounds #47's `asyncio.to_thread` fan-out
**Addressed.**
- (a) `pool_size=1000` restored on both `_engine` and `_engine_sync`
  (`persistent.py:174,178`), with a comment explaining the cost that was being
  incurred (SQLAlchemy QueuePool checkout blocking the GUI thread, not sqlite's
  lock).
- (b) The `asyncio.to_thread` sites in `mark_sent` are bounded with
  `_MARK_SENT_THREAD_SEM = asyncio.Semaphore(8)`, so a write burst can't
  saturate the shared default thread pool (`persistent.py:367`, `399/420/433`).

`b2f3b0c`.

### 4. Read-side "prompt retry" promise broken end-to-end (no-op event)
**Addressed.** The read path's `give_up()` now also sets `__mixwal_updated`
(`network.py:467`), which `drain_mixwal2` consumes via its FIRST_COMPLETED wait
and re-scans `MixWAL.get_new()` immediately — so a give-up (CourierError /
epoch-staleness / transient busy) is re-cast promptly instead of waiting out the
~15s poll. `readables_to_mixwal_event` alone stays a no-op as the comment now
explains. `2cad52d`.

---

## PR #45 — 3party (voucher: multi-party support)

### 1. Unlocked COUNT(*) order-assignment races the GUI send path → IntegrityError
**Addressed.** `send_introduction_message`'s log-append now runs under the same
per-conversation `conversation_log_order_lock(conversation_id)` used by the GUI
send path and the receive drain (`voucher.py:387`; `persistent.py:119`;
`network.py:624`), serialising the count/insert/commit critical section. The
subquery itself is deduplicated (see D1). `6bc6aa0`.

### 2. `_already_has` matches on display_name alone → name collisions hide peers
**Addressed.** `_already_has` now matches *only* on the unique read cap
(`conversation_handlers.py:81`), so a distinct member sharing a display name
(including the reader's own name) is never silently skipped. Self-recognition
is also fixed for the un-provisioned-cap race (own cap falls back to the
unmutated `ReadCapWAL.read_cap` before comparing: `conversation_handlers.py:65`).
`6218ffe`.

### 3. Docstring says "fire-and-forget" but blocks the caller up to 180s
**Addressed.** `await _wait_intro_acked(...)` is replaced with
`create_task(_wait_intro_acked(...))`; the caller returns immediately, and the
ack-wait (still bounded at 180s) runs in the background and only ever logs
(`voucher.py:449`, `452`). `6bc6aa0`.

### 4. No "already inducted" guard — naive retry creates a duplicate peer
**Partially addressed — split:**
- (a) **Addressed:** a failure in the post-commit announcement no longer
  surfaces as "induction failed". `send_introduction_message` catches every
  exception, logs, and returns, and never blocks the caller — so the unsafe
  retry-tempting *signal* is gone (`voucher.py:427-434`). `6bc6aa0`.
- (b) **TODO:** there is still no literal "already inducted" guard.
  `_add_peer` (`voucher.py:243`) dedups on neither name nor read cap, so an
  explicit re-run of `derive_read_and_induct` (e.g. after a crash between the
  commit and `send_introduction_message`) would still insert a duplicate peer /
  re-issue the INTRODUCTION.

### 5. Epoch-staleness retry only in voucher.py; chat read path unpatched
**Addressed.**
- (a) `voucher._read_box`'s transient list widened to `DatabaseFailureError`,
  `CourierError`, `ThinClientOfflineError`, with a stall-visible WARNING after
  `_STALL_WARN_ROUNDS` rounds (`voucher.py:200`). `6bc6aa0`.
- (b) The ordinary chat read path is covered: `drain_mixwal_read_single` treats
  BoxIDNotFound/Tombstone/DatabaseFailure/CourierError as benign give-ups
  (stale-epoch `CourierInvalidEpochError` is a `CourierError` subclass), and
  the cross-stack #4 fix makes that give-up re-cast the read promptly instead
  of silently deferring to the poll. `2cad52d`.
- (c) New direct coverage of `_read_box`'s retry loop incl. the transient types
  and stall warning. `cef0572`.

### 6. `own_read_cap` falls through to None → TypeError crashes the joiner
**Addressed.** Both sides are guarded: `_build_who_reply` omits self from the
who-reply when the own cap is unprovisioned (`voucher.py:528-541`), and
`_add_peer` refuses a `None`/malformed read cap instead of slicing it
(`voucher.py:243`). `6bc6aa0`.

### 7. `peer_added_listener`'s `while ... not in conversation_state_by_id: sleep(1)` has no timeout
**Addressed.** The wait is now bounded by `_wait_for_conversation_state`
(`_CONVERSATION_STATE_WAIT_TIMEOUT_S = 30`), which logs and *skips* the item on
timeout — a stale/deleted conversation_id no longer starves every later
notification (`katzen.py:523`, `596`). Applied to `receive_msg_listener` too.
`ddf2d32`.

### Duplication findings
The review says "five duplication findings" but names three idioms; the three
named ones are all resolved:

- **D1 order-assignment idiom** — **Addressed:** `persistent.next_conversation_order`
  is now the single appender used by `append_outbound_chat`, `voucher._write_introduction_log`,
  and `ConversationLog.append_from` (`persistent.py:89`). `ea31ea2`.
- **D2 ack-wait poll idiom** — **Addressed:** `persistent.wait_for_sent`
  (`persistent.py:436`) replaces voucher's `_wait_intro_acked` loop and the
  headless SEND step's inline poll. `b1698e5` (+ `4c2a4d9` wiring headless to it).
- **D3 INTRODUCTION-detection check** — **Addressed:** `GroupChatMessage.as_introduction`
  (`models.py:235`) centralizes the (msg_type, introduction-present) test used
  by `qt_models.py` row rendering and the headless READ step. `5f7f736`.

**TODO (note rather than code):** REVIEW.md claims *five* duplication findings
but only names these three; if two intended duplication findings were dropped
from the review text, they are not recoverable from this document.

---

## PR #47 — fix-sqlite-write-wedge

### 1. Read-drain task has no exception safety net
**Addressed.** Covered by cross-stack #1: `_on_read_done` releases the stream
and logs at ERROR with `exc_info` on any unhandled read-task exception
(`network.py:832`). `2cad52d`.

### 2. Two early-return branches never discard from `draining_right_now`
**Addressed.** The "not advancing idx / probably already handled" branch
(`network.py:572`) and the "invalid prefix → deactivate peer" branch
(`network.py:588`) now both `draining_right_now.discard(bacap_uuid)`; a plain
duplicate/late MW no longer wedges the stream. `2cad52d`.

### 3. Attachment spill not covered by its transaction → orphaned file on retry
**Addressed.** `_spill_attachment` now derives the filename from the content's
sha256 and reuses an existing file when present, so a retried commit writes
(and orphans) no second copy (`network.py:341-359`). Idempotency unit test
added. `56ce966`.

### 4. `except OperationalError` catches by type only → invariant bugs retried forever
**Addressed.** Both drain catches are gated by `_is_transient_sqlite_busy` —
only "database is locked" is treated as transient; schema drift / readonly DB /
other OperationalError re-raise and surface (`network.py:95`, `205`, `691`).
`2cad52d`.

### 5. font_settings_dialog's GUI-thread commit has no exception handling
**Addressed.** The AppSetting write in `_apply_font_settings`'s path is wrapped
in try/except with a warning (persistence failure no longer crashes the GUI;
the in-memory settings already applied) (`katzen.py:317-330`). `ddf2d32`.

### 6. Alembic migration engine never gets `_set_sqlite_pragmas`
**Addressed.** `migrations/env.py` registers `_set_sqlite_pragmas` on the
migration engine's sync engine, so a live-app migration waits out contention
instead of failing fast (`env.py:88-91`). `b2f3b0c`.

### Reuse findings
- **R1 `_on_write_done` re-implements `on_error` from scratch** — **TODO.**
  `_on_write_done` (`network.py:749`) and `on_error` (`network.py:968`) remain
  two separate bespoke done-callback implementations. They now log alike
  (both `logger.error(... exc_info=...)`), but they are not unified.
- **R2 finalize block duplicated between `_finalize_stale_ack` and `_mark_sent_txn`**
  — **Addressed:** extracted into `_ensure_sent_log_and_flip_status`
  (`persistent.py:472`), used by both. `b2f3b0c`.
- **R3 new `asyncio.to_thread` sites have no semaphore** — **Addressed:** all
  three `mark_sent` to_thread calls run under `_MARK_SENT_THREAD_SEM(8)`
  (`persistent.py:367`). `b2f3b0c`.

---

## PR #48 — deckard-wip (hardened drain loop)

### 1. 120s read watchdog fires on ordinary idle conversations
**Addressed.** `READ_WATCHDOG_SECONDS` raised to 1200s and demoted to a
last-resort backstop; the primary defence now races the read against a
**daemon-reconnect** event with a 30s grace (`_await_read_reply`,
`network.py:247-282`), which is the only signal that a reply could have been
orphaned, and treats the outcome as recoverable (cancel ARQ + re-schedule),
not an error. `2cad52d`.
*Residual:* a conversation silent for >1200s with no observed reconnect still
trips the backstop, but benignly.

### 2. Done-callback no longer re-raises → listener errors die invisibly
**Partially addressed — split:**
- (a) **Addressed:** `create_task`'s done-callback now logs the exception via
  `logger.error(..., exc_info=exc)` instead of a bare `print`/traceback
  (`katzen_util.py`), and `on_error` does the same (`network.py:985`). The
  concrete bugs that would actually kill the listeners — `AttributeError` on a
  not-yet-started `kp_client` (`katzen.py:1008`) and the unbounded
  `conversation_state_by_id` wait (see #45-7) — are fixed. `ddf2d32`.
- (b) **TODO:** `receive_msg_listener` / `peer_added_listener` are still bare
  `while True:` loops (`katzen.py:544`, `588`) with no internal try/except; an
  unexpected exception still terminates the loop, only now loudly logged. No
  re-raise, dialog, or restart.

### 3. `drain_mixwal2` snapshots connectedness before a ~15s wait
**Addressed.** `connected = __mixnet_connected.is_set()` is read fresh *after*
the `asyncio.wait` (`network.py:811`), and `on_connection_status` additionally
pokes `__mixwal_updated` on a reconnect so a deferred write goes out on that
pass rather than waiting another sweep (`network.py:1127`). `2cad52d`.

### 4. `on_error`'s done-callback drops its raise → debug-only visibility
**Addressed.** Now `logger.error("on_error: task failed: %s", e, exc_info=e)`,
visible at the default level, while still not re-raising (avoids spurious
asyncio callback tracebacks for transient link drops — the docstring explains
the trade-off) (`network.py:979-988`). `2cad52d`.

### 5. Stale comment claims every ConversationLog append funnels through the single writer
**Addressed.** The misleading "every append funnels" comment was rewritten to
describe the actual split: async-engine appends funnel through the io-loop
writer via `run_in_io`, while one-shot GUI-thread commits (`new_conversation`)
use the separate `_engine_sync` which has no event-loop affinity — with the
risk noted (pool_size + busy_timeout bound the stall) (`persistent.py:31-50`;
`katzen.py:673`, `892`). New path `network.notify_outbound_chat_sent` folds
append + queue-put + `check_for_new` into a single io-loop hop (`network.py:58`).
`ddf2d32`.

### Test-quality notes
- **T1 watchdog-recovery test's assertion passes trivially** — **Addressed:**
  the un-stuck pass now re-adds the stream to `draining` first so the success
  path's own discard is really exercised, and a new
  `test_lost_read_reply_is_recovered_after_reconnect` asserts real recovery
  after a reconnect mid-wait (`tests/test_network_fake.py`).
  `b0185dc` + `2cad52d`.
- **T2 concurrent-write regression test never calls the guarded function** —
  **Addressed:** `_append_log` now calls the real
  `persistent.append_outbound_chat(...)` instead of a hand-rolled copy of its
  lock/count/commit sequence, plus a new cross-OS-thread lock test
  (`tests/test_concurrent_write_orders.py`). Also fixes the stale
  asyncio-vs-threading lock comment in `tests/conftest.py`. `b0185dc`.
- **T3 forensic diagnostic unreachable on its failure path** — **Addressed:**
  `test_restart.py` now runs the forensic `_snapshot_role_state` on the
  `except Exception` path (the timeout case it exists to diagnose), and the
  leaked-shared-container cleanup catches `Exception`, not just
  `subprocess.TimeoutExpired`; duplicated integration helpers deduped into
  `tests/integration/_bounce_helpers.py`. `2bf216f`.

---

## PR #53 — misc-fixes

### 1. Dropping `pool_size=1000`/`echo=True` collapses to SQLAlchemy's default pool
**Addressed.**
- (a) `pool_size=1000` restored on both engines (see cross-stack #3).
- (b) Verbose SQL echo restored conditionally (on `KQT_LOG_LEVEL`), instead of
  the engines silently defaulting it off (`headless/__init__.py:170-186`).
  `b2f3b0c` + `4c2a4d9`.

### 2. Disconnect warning logs on every failed reconnect attempt, not per transition
**Addressed.** `on_connection_status` tracks `_last_connected` and warns only on
the disconnect transition (or the first report ever), also suppressing the
warning when an `err` payload already lands in the ERROR log (`network.py:1112-1145`).
Regression tests assert exactly one warning across repeated identical reports,
and no warning on a disconnect-with-err. `2cad52d` + `b0185dc`.

### 3. `cli()` comment references the old `echo=True` mechanism, now false
**Addressed.** The comment was rewritten and the echo toggle inverted to
"enable on verbose", matching the new symmetric on/off branches, and — a bonus
unrelated to the review — the READ step's optional deadline suffix no longer
truncates target text containing a colon (`headless/__init__.py:118`,
`_actions.py:_parse_read_step`). `4c2a4d9`.

---

## Open TODOs (summary)

1. #45-4b — no "already inducted" guard: explicit re-run of an induction
   (`_add_peer` / `derive_read_and_induct`) can still create a duplicate peer.
2. #47-R1 — `_on_write_done` and `on_error` remain separate done-callback
   implementations (not deduplicated).
3. #48-2b — `receive_msg_listener` / `peer_added_listener` remain bare
   `while True` loops: an unexpected exception still kills the loop (now loudly
   logged; no re-raise/restart/dialog).
4. #45-dup-note — REVIEW.md's "five duplication findings" count vs. the three
   idioms it names (informational; nothing to fix in code).