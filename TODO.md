# Active backlog

## `conversation_order` / message deletion

- [ ] **ConversationLog: assign `conversation_order` from `MAX(order)+1` instead of
      `COUNT(*)`, so message deletion doesn't corrupt ordering.**
      `persistent.next_conversation_order()` (`persistent.py:94`) returns a live
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

