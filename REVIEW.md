The document below is a review of the series of commits main..deckard-wip
(which is actually a stack of five PRs, who's numbers are referred to in the
review text below). I want to know which of these review findings are resolved
by the commits on deckard-wip..review-findings-fixes so: your task is to create
a new file here called REVIEW2.md which enumerates the action items in this
review. For each actionable item in REVIEW.md, in REVIEW2.md you should write
TODO and note what remains to be done, and/or note which commit(s) on the
review-findings-fixes branch it was addressed in. Do not run any tests, only
read the code and the review. You can fetch the PR refs from github (using git,
not the gh tool) to be able to understand the references to PR numbers in the
review.

---

The review:

All five PRs in the stack are accounted for now (#46 already merged, not re-reviewed). Here's the consolidated verdict, worst first, with the cross-PR interactions that matter before you look at any single diff.

Cross-stack interactions worth knowing first

- PR #47's biggest gap is partially closed by #48, but not fully. #47 leaves drain_mixwal_read_single's fire-and-forget task with no _on_write_done-equivalent safety net (an uncaught exception wedges that stream forever). #48 does add an _on_read_done closure for exactly this, but drops the exc_info error logging that _on_write_done has, so a stuck-then-recovered read stream is now silent where the write-side equivalent still logs.
- #47's asymmetric OSError handling is still asymmetric after #48. #48's stated goal was to widen the give-up exception list to include OSError, but it only did so for the read path. The write path (drain_mixwal_write_single) still lacks it, and worse, _on_write_done retries with zero backoff on that class of error, unlike every other transient-error path in the file.
- #53 removes pool_size=1000 right on top of #47's new asyncio.to_thread fan-out in mark_sent. #47's own reviewer flagged the thread-pool contention risk but assumed "pool_size=1000 suggests much more headroom." #53 then deletes that pool_size, collapsing to SQLAlchemy's default (5 + 10 overflow). Combined, a write burst now risks exactly the GUI-thread stall #53's docstring says the 250ms busy_timeout was designed to bound, since that bound covers SQLite's lock wait, not a pool-checkout wait.
- The read-side "prompt retry" promise is broken end-to-end. #47's read-path give_up() sets readables_to_mixwal_event, but that event's consumer query filters out streams that still have a MixWAL row, exactly the state left behind by a rollback, so it's a no-op. Retry only happens on the ~15s poll timeout. Neither #48 nor #53 touches this.

PR #45 — 3party (voucher: multi-party support)

Most serious:
1. send_introduction_message's unlocked COUNT(*) order-assignment races the GUI send path and can IntegrityError-abort an induction that already succeeded on the wire. (Note: this exact race is what #46, already merged, was written to close for other call sites, this one's a leftover.)
2. _already_has matches on display_name alone, no uniqueness enforced, can silently hide a peer from a member whenever two names collide (including the reader's own name).
3. Docstring says "fire-and-forget" but send_introduction_message blocks the caller up to 180s via _wait_intro_acked.
4. No "already inducted" guard, a failed post-commit ack makes a naive retry create a duplicate peer.
5. The epoch-staleness retry fix only lives in voucher.py, the ordinary chat read path has the identical daemon-hang exposure, unpatched.
6. own_read_cap can fall through to None and crash the joiner with a TypeError.
7. peer_added_listener's while ... not in conversation_state_by_id: sleep(1) has no timeout, one bad id starves every future notification.

Plus five duplication findings (the order-assignment idiom, the ack-wait poll idiom, the INTRODUCTION-detection check, each independently reimplemented 2-3 times across files).

PR #47 — fix-sqlite-write-wedge

Most serious:
1. Read-drain task has no exception safety net (see cross-stack note above, #48 half-fixes it).
2. Two early-return branches in drain_mixwal_read_single never discard from draining_right_now at all, no exception needed, a plain duplicate/late MW permanently wedges that stream.
3. Attachment spill-to-disk isn't covered by the transaction it sits inside, a retried commit orphans the file and writes a second copy.
4. except OperationalError catches by type only, a genuine schema-drift or readonly-db error gets treated as "transient" and retried forever instead of surfacing.
5. font_settings_dialog's GUI-thread commit has no exception handling at all, unlike theme.py's equivalent sites, first commit into the exact race this PR targets.
6. Alembic's migration engine never gets _set_sqlite_pragmas applied, running a migration while the app is live can hit an immediate lock error.

Plus reuse findings: _on_write_done reimplements the existing on_error helper from scratch; the finalize block is duplicated verbatim between _finalize_stale_ack and _mark_sent_txn; the new asyncio.to_thread sites have no semaphore despite the project's own documented convention for exactly this kind of stream.

PR #48 — deckard-wip (hardened drain loop)

Most serious:
1. The new 120s read watchdog fires on every ordinary idle conversation, not just stuck ones, because the daemon's own uncapped-retry behavior for a quiet peer is by design, not a hang.
2. create_task's done-callback no longer re-raises, which silently kills the GUI's error dialog for receive_msg_listener and peer_added_listener, both bare while True loops with no internal handling. Any bug in either now just dies invisibly.
3. drain_mixwal2 snapshots the connection-state flag before a ~15s wait and doesn't re-check it, a reconnect mid-wait can delay write dispatch up to 30s versus the old edge-triggered wait it replaced.
4. on_error's done-callback also drops its raise, downgrading a real bug's visibility to a debug-only log line under the default level.
5. A stale comment claims every ConversationLog append is funneled through the single writer, but new_conversation still commits directly on the Qt loop, the exact cross-loop hazard this PR exists to close.

Test-quality notes worth a look: the new watchdog-recovery test's assertion passes trivially regardless of correctness; the new concurrent-write regression test never actually calls the function it's meant to guard; a forensic diagnostic added specifically for one failure path is unreachable on that exact path because the code re-raises before reaching it.

PR #53 — misc-fixes

1. Dropping pool_size=1000/echo=True collapses to SQLAlchemy's default pool, compounding with #47 as noted above.
2. The new disconnect warning logs on every failed reconnect attempt, not once per transition as the commit message claims (test only covers a single call, doesn't catch this).
3. cli() still references the old echo=True mechanism in a comment that's now false.

