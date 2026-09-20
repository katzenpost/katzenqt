# Old TODO backlog

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

## old `conversation_order` TODO items

These are obsolete but preserved here for now for review.

- [ ] **ConversationLog: assign `conversation_order` from `MAX(order)+1` instead of
      `COUNT(*)`, so pruning/deleting messages won't corrupt the ordering.**
      Motivation: we plan to implement message deletion soon, and we avoid migrations.
      Today `conversation_order` is stamped with a `select(count())` scalar subquery
      evaluated in the INSERT at commit, race-free only because all appends serialize
      under the per-conversation lock (`conversation_log_order_lock`). If any
      `ConversationLog` row is ever deleted, COUNT under-counts and the next append
      collides with the highest surviving order -> UniqueConstraint(conversation_id,
      conversation_order) trips -> a silently dropped message. This is a query-only,
      no-migration change (behavior-identical to COUNT while nothing is deleted:
      after the create-conv order=0 row, COUNT == MAX+1 == next index).
      Change all three sites from `count()` to `coalesce(func.max(order), -1) + 1`:
        - `ConversationLog.append_from`      src/katzenqt/persistent.py (~713)
        - `persistent.append_outbound_chat`  src/katzenqt/persistent.py (~112)
        - induction append                   src/katzenqt/voucher.py (~381)
      Imports: persistent.py currently selects `count()`; add `sqlalchemy.func` /
      `coalesce`; voucher.py uses `select(persistent.count())`, switch to `func.max`.
      The per-conversation lock STAYS (MAX+1 removes the delete-vs-append hazard, not
      the append-vs-append race). Tests: add a unit case that deletes a middle row
      then appends and asserts new order == old MAX+1 with no unique violation; the
      existing same-loop contention regression in test_voucher_guard.py must still
      pass. NOTE for the deletion feature itself: deleting rows creates gaps and the
      UI's `conversation_order == index_row` lookup (qt_models.py:150) assumes a
      gapless 0-based index — the deletion feature must enumerate by order query, not
      index into it.

- [ ] **(Deferred; needs migration) Make `conversation_order` assignment atomic and
      app-lock-free via a per-conversation counter table.**
      The MAX+1/lock design above is correct in one process but fragile by
      convention: every append site must remember the funnel + per-conversation lock,
      and the lock does nothing if a second process ever appends the same
      conversation (a future GUI + headless worker on one state file). If we ever
      need that, the clean design is: one row per conversation in a counter table,
      and inside the append transaction do
      `UPDATE counter SET next = next + 1 WHERE conversation_id = ? RETURNING next`,
      then INSERT ConversationLog with that literal value. SQLite serializes the
      UPDATE, so it is correct in-process and cross-process with no app lock and no
      funnel dependency, and it composes with the MAX+1 deletion-safety above.
      Cost: a new table = a migration, which we deliberately avoid (an upgrade with a
      migration can't be rolled back by users). Do this only if/when the
      single-process assumption breaks or a forgotten-lock bug bites again.

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

