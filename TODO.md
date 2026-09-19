# Tally GUI TODO

## Maintenance rules for this file

This file is a living document and is committed to git (branch
`deckard-tally`).

Keep it up to date as part of the working session:

- Whenever a TODO item is completed, mark it done (move it to a "Done" /
  "Resolved" section or update its status) and **commit** the change.
- Whenever new discoveries obsolete or supersede statements in this file,
  update the statements and **commit**.
- Whenever new tasks surface, add them (either marked as new/backlog or with a
  note about their priority) and **commit** if they are "session-worthy".
  Small ephemeral notes that will be consumed within the same session can
  stay uncommitted until the session ends.
- Commit messages for changes to this file should be short and match the repo's
  existing style (imperative, lowercase-ish, e.g. `update TODO.md: ...`).
- Keep TODO.md commits **separate** from commits that change other files
  (code, tests, etc.). When completing an item, first commit the work itself,
  then commit the TODO.md status update. This keeps the non-TODO commits
  cherry-pickable on their own.

## Goal

Add a GUI for the existing tally/poll protocol (already fully implemented in
`src/katzenqt/tally/`, tested, and wired into the receive path). Today the
protocol only surfaces through the headless CLI verbs
(`src/katzenqt/headless/_actions.py`: `tally-create`, `tally-vote`,
`tally-result`, `tally-close`, `tally-list`). The GUI app (`katzenqt`) has no
way to see, create, vote on, or close a survey.

## Decisions (locked with the user; do not reopen without asking)

1. Poll placement: in-chat placeholder rows + a separate poll detail/vote panel.
2. Poll panel lives embedded in the main window as a **sibling tab in the chat
   tab bar** (alongside the chat / single/multi/PTT tabs).
3. Create-poll flow is **one hybrid dialog**: tab 1 "Custom options"
   (topic + mode + manual slot list), tab 2 "Dates" (pick dates that become
   slots). Reuse the same shared slot-list widget for both tabs.
4. Voting widget is a **click-to-cycle grid**: per slot a button that cycles
   blank -> yes -> maybe -> no where the mode allows (see
   `tally/schema.py::domain`). A recast mints the next version
   (`tally/engine.py::current_version`).
5. Live updates arrive via a **tally notification queue** (mirror of
   `network.py::conversation_update_queue`) that refreshes models/badge and
   reuses the existing unread/message-alert path (badge count on the Polls tab).
6. Gap fixes in scope: a **per-voter detail view** in the panel, **fix the
   malformed sync-request crash** in `tally/controller.py`, and **advisory
   close handling** in the UI (a received TALLY_CLOSE must flip the panel/model
   to "closed" and disable voting).
7. **Catch-up / Refresh (TALLY_SYNC_REQ) button is DEFERRED.** Underlying
   protocol supports it (`tally/sync.py`, `headless/_actions.py` has
   `tally-resync` machinery); wiring a GUI button later is a contained change.
   Rationale: a true late joiner already reads every member stream from box 0
   (member read caps embed the stream's `first_message_index`, so full history
   is readable when the daemon still retains the boxes); TALLY_SYNC_REQ only
   repairs offline windows / pruned boxes / dropped messages.
8. Placeholder rows are **virtual and GUI-derived**: no new ConversationLog
   rows, no writes on the Qt loop. A Qt-only model merges placeholders in.
9. Placeholder rows are **clickable** and open that survey in the poll panel
   (raising the Polls tab).
10. Testing: **both** Qt-free presenter/engine unit tests *and* offscreen Qt
     widget/model tests (`QT_QPA_PLATFORM=offscreen`, pattern from
     `tests/test_conversation_log_model.py`).
11. The Polls sibling tab lists the **currently selected conversation's**
     surveys only (the in-chat placeholders already show that conversation's
     surveys), and its badge counts that conversation's new polls.

## Architecture constraints (must hold)

- `tests/test_qt_decoupling.py` forbids these modules from importing PySide6:
  `katzenqt` (package), `persistent`, `network`, `models`. The `tally/` package
  and any new presenter must stay Qt-free.
- Qt-only UI code lives in Qt-only modules like `qt_models.py` (which may import
  PySide6 + `katzenqt.persistent`). New Qt code goes in `katzenqt/qt_tally.py`.
- Two event loops: Qt loop + `AsyncioThread` io loop (`katzen.py`).
  DB/network writes and async reads happen on the io loop
  (`iothread.run_in_io(...)`); the Qt loop may do **sync** reads via
  `persistent.Session(persistent._engine_sync)` (precedent: qt_models.py's
  `ConversationLogModel.data`, and the `lru_cache`-on-data pattern).
- The protocol's five message kinds are handled in
  `conversation_handlers.py::_HANDLERS`; tally messages go to
  `tally.controller.handle_event`, NEVER become ConversationLog rows
  (`convlog_added=False`), and today never notify the GUI (the gap this work
  closes). TallyState row = one survey = one pycrdt Doc blob; everything
  (topic, mode, slots, status, votes) is derived from the Doc, not stored.

## Key code locations

- Reference documentation for the tally feature: `docs/tally-api.md`
  (protocol/API reference) and `docs/tally-howto.md` (task recipes, including
  the exact session-scoped create/vote/close patterns step 5 wires). NOTE the
  directory is `docs/`, not `doc/`; these two .md files are important context
  for this work.
- Receive dispatch / order lock: `network.py` `drain_mixwal_read_single`,
  holds `conversation_log_order_lock(notify_conv_id)` at `network.py:994`;
  dispatches per message at `network.py:1019` (substream) and `:1038`
  (top-level). Post-commit notifications at `:1103-1117`
  (`conversation_update_queue`, `peer_added_queue`, `check_for_new`).
- Routing: `conversation_handlers.py::_handle_tally` (`:167`) ->
  `tally.controller.handle_event` (`controller.py:193`).
- Order assignment: `persistent.py::next_conversation_order` (`:94`) and
  `ConversationLog.append_from` (`:915`). UniqueConstraint per
  (conversation_id, conversation_order) is on the log table only.
- TallyState table: `persistent.py:928` (cols: survey_id PK, conversation_id,
  doc_state). Needs a new nullable `conversation_order` column (migrations live
  in `src/katzenqt/migrations/versions/`; alembic configured in pyproject).
- Doc accessors: `tally/schema.py` (meta/votes maps, mode_of, status_of,
  survey_id_of, topic_of, creator_of, slots_of), `tally/engine.py` (tally(),
  outcome(), apply_vote, close_survey, current_version), `tally/sync.py`,
  `tally/events.py` (build_create/build_vote/build_close).
- Voter identity: `tally/controller.py::voter_id_from_read_cap` (blake2b of the
  read cap, 16 bytes) and `_voter_id()` (`:44`). Name mapping for the per-voter
  view: join peer.read_cap_id -> ReadCapWAL.read_cap -> hash (same derivation).
- GUI wiring: `katzen.py` MainWindow; `qml_ctx()` feeds `chatTreeViewModel`
  (`qt_models.py:457`), `add_conversation` wires the ConversationLogModel
  (~katzen.py:1901), `conversation_selected` (~katzen.py:1461).
  The QML chat is `resources/chatview.qml` (TreeView + DelegateItem, unread
  Timer ~1500ms, `first_unread`).
- Qt model pattern: `qt_models.py:241` `ConversationLogModel`
  (roleNames, lru_cache'd index/data, row_count + beginInsertRows).
  Roles start at `ROLE_CHAT_AUTHOR = 0x100`.

## Implementation steps

### 1. Order capture for placeholder placement (Qt-free)

- Add nullable int column `conversation_order` to `TallyState`
  (`persistent.py:928`) + alembic migration (new revision, batch edit,
  nullable=True; older surveys keep NULL).
- Compute the value at ingest inside `_handle_tally`
  (`conversation_handlers.py:167`): call
  `persistent.next_conversation_order(conversation_id)` (the receive loop
  already holds `conversation_log_order_lock`), pass it into
  `controller.handle_event(...)`, and store it in `_save()`. The timeline
  placeholder then lands exactly where the event was read. Order numbers
  "consumed" without a log row are harmless (uniqueness is log-row-only).
- Update `headless/_actions.py` call sites / tests that construct TallyState.

### 2. Gap fixes (Qt-free)

- Malformed sync-request crash (`controller.py:256-262`): wrap
  `sync.diff_since(doc, tally.crdt or b"")` in `try/except ValueError` -> log +
  drop (return False) instead of raising into the receive loop. Add a unit test
  in `tests/test_tally_sync.py`.
- Per-voter detail: add a pure `engine.per_voter(doc)` (mirror `engine.tally`)
  returning per-voter_id -> {slot_id: availability, version}. The presenter
  joins voter_id -> peer display name using the same read-cap hash derivation.
  Extend `tests/test_tally_engine.py`.

### 3. Qt-free presenter — `src/katzenqt/tally/presenter.py`

Pure functions, no PySide6, unit-testable:
- `survey_summary(doc, conversation_order, is_new)` -> topic, mode, status,
  n_voters, my_vote, outcome, creator, tin.
- `row_text(summary)` -> the placeholder line shown in the chat timeline
  (e.g. `[Polls] <topic> - open, 2/4 voted`).
- `panel_state(...)`, `badge_count(surveys)`.
Read supplied data via sync `persistent.Session(_engine_sync)` reads as needed
(same precedent as qt_models).

### 4. Qt-only UI — `src/katzenqt/qt_tally.py`

- `TimelineModel(QAbstractItemModel)`: wraps a per-conversation
  `ConversationLogModel` and interleaves ordered virtual placeholder rows.
  Forward `data()` for the existing roles (0, ROLE_CHAT_AUTHOR,
  ROLE_CHAT_NETWORK_STATUS, ROLE_CHAT_MESSAGE_ID, attachment +) to the wrapped
  model; placeholder rows render via `presenter.row_text`. New roles:
  `ROLE_TALLY_PLACEHOLDER`, `ROLE_TALLY_SURVEY_ID`, `ROLE_TALLY_NEW`. React to
  the wrapped model's row insertions so QML's unread/scroll logic (row-count
  growth per event, `first_unread`) keeps working.
- `PollsTabModel(QAbstractListModel)`: rows = surveys (per current
  conversation, or all); roleNames for topic/status/mode/n_voters/my_vote/
  new/survey_id; drives the Polls tab and the badge count.
- `TallyPanel(QWidget)`: header (topic, creator, status, mode), outcome line
  (`engine.outcome`), click-to-cycle vote grid, "Send vote" button (stages via
  `controller.cast_local_vote` + `tally.send` on the io loop; recast = new
  version), per-voter detail section, creator-only "Close" button
  (`close_local` + build_close). Status flips to closed on a received
  TALLY_CLOSE (via notification queue).
- `TallyCreateDialog(QDialog)`: two tabs both editing a shared slot-list
  widget; tab "Dates" uses a `QCalendarWidget` (single-select) +
  "Add selected date as slot". Create sends via `create_local` +
  `build_create`.
- Expose the queue: `tally_update_queue` (async, in `network.py` alongside
  `conversation_update_queue`) pushes (conversation_id, survey_id) on each
  received tally event; a Qt-side queued connection refreshes
  TimelineModel/PollsTabModel + tab badge. Also push on local create/vote so
  the UI reflects own sends.

### 5. Wiring (`katzen.py` MainWindow)

- Add the Polls tab programmatically:
  `chatTabs.addTab(TallyPanel(...), "Polls")`; badge via tab text
  ("Polls (3)"). No `.ui` edit required (verify `chatTabs` is the right
  QTabWidget in the built window; regenerating ui_mixchat.py is otherwise via
  `make`, see HACKING.md).
- Swap `chatTreeViewModel` / the per-conversation model in `add_conversation`
  and `qml_ctx()` to the `TimelineModel` wrapper.
- Wire placeholder click: `chatview.qml` delegate renders placeholder rows with
  a distinct style and emits an `openPoll(surveyId)` signal; `katzen.py`
  connects it to a slot that selects the survey in the panel, raises the Polls
  tab, and clears the row's `new` flag (same pattern as `first_unread` update).

### 6. Tests

- Qt-free: `tests/test_tally_presenter.py` (summaries / row_text / badge);
  extend `tests/test_tally_engine.py` (per_voter); `tests/test_tally_sync.py`
  (malformed sync-req does not raise); controller test for order capture.
- Offscreen Qt (pattern from `tests/test_conversation_log_model.py`:
  `QT_QPA_PLATFORM=offscreen`, `QApplication`/`QGuiApplication` fixture):
  `tests/test_qt_tally.py` exercising `TimelineModel` merge/roles,
  `TallyPanel` click-cycle -> submit, `TallyCreateDialog` slot building,
  badge computation.
- Migration bootstrap test (`tests/test_runner_migration_bootstrap.py`) must
  cover the new `TallyState.conversation_order` column.
- Run the unit suite with the repo's normal commands (see HACKING.md / Makefile);
  integration tests are docker-gated (`KATZENQT_DOCKER_INTEGRATION=1`) and do
  not need to run for this GUI work.

## Accepted tradeoffs / notes

- Pre-existing surveys with NULL conversation_order fall back to
  arrival-order placement in the timeline (cosmetic).
- No native multi-date `QCalendarWidget` selection in Qt 6.9/6.11; the Dates tab
  works around it by single-select + "add" per the locked decision.
- Catch-up/sync-req button deliberately deferred (see Decision 7).
- Protocol semantics, persistence format, and headless verbs are unchanged; this
  work only adds a GUI-facing view over the existing controller.

## Current working plan (session restarted 2026-09-19)

The previous session committed Step 3 (`83fd5ef`) but never marked it done
here, drifted into Step 4 (receive-path tally notification plumbing, presenter
read helpers, and an untracked `qt_tally.py`) while uncommitted, then lost the
thread. This file is being kept up to date **continuously** this time: every
sub-phase lands as its own code commit, with the matching TODO.md status
committed separately (same message style: `update TODO.md: ...`).

Remaining sequence, with a review checkpoint after Step 4:

1. [x] Commit the working-tree Step-4 backend as-is: the `tally_update_queue`
   receive-path plumbing (`network.py`, `conversation_handlers.py`, conftest
   reset, handler/controller test updates, network-fake test), then the
   presenter read helpers (`survey_doc`, `first_unread_order`,
   `conversation_names`) and their tests. (commits 81d83c9, 6558dbd)
2. Finish Step 4 in `qt_tally.py`: review the untracked module for ordering /
   unread-mapping / role-forwarding correctness; add `TallyPanel.show_survey`
   plus current-survey tracking and a "New poll" dialog host; scope
   `PollsTabModel` to the current conversation (locked Decision 11). Add
   `tests/test_qt_tally.py`. Run `make test-uv`.
3. **PAUSE for user review** once Step 4 is committed.
4. Step 5 wiring (`katzen.py` + `resources/chatview.qml`): Polls tab +
   badge, `TimelineModel` swap (route `increment_row_count` / `redraw` /
   `row_count` to the wrapped source), first-unread row<->order mapping,
   `tally_update_queue` listener, io-loop create/vote/close (pattern from
   `headless/_actions.py`), QML placeholder rows + `openPoll(surveyId)`.
5. Step 6 remainder: migration-bootstrap coverage for the
   `TallyState.conversation_order` column, full unit run.
6. Small docs update once the GUI lands: `docs/tally-api.md` (dispatch now
   returns a 4-tuple `(convlog_added, signal_send, peer_added, tally_added)`,
   the tally notification queue, "no GUI yet" and Known gap #1 are obsolete)
   and `docs/tally-howto.md` (the "no tally notification channel" section).

## Session log

- [x] Confirmed with the user the protocol/persistence model, resolving the
      catch-up open question: late joiners read each member stream from box 0
      (read caps embed `first_message_index`; `voucher.py::_build_who_reply`,
      `_add_peer` seeded `next_index = read_cap[-104:]`), so true late joiners
      receive prior polls; TALLY_SYNC_REQ is edge-case repair -> deferred.
      Corrected a wrong earlier claim: tally messages ARE persisted (as CRDT
      Doc blobs in TallyState), just not as ConversationLog rows.
- [x] Locked all 11 design decisions (see Decisions above).
- [x] Verified schema, receive path, expect blocks, model patterns, test style.
- [x] Step 1: order capture. Added nullable `TallyState.conversation_order`
      (persistent.py + migration `3b19e0386cdd`); `_save` stamps it with
      `next_conversation_order` **only on first insert** (later votes/closes
      keep the placeholder pinned to first-sighting). Receive path already
      calls us under `conversation_log_order_lock`. Test
      `test_survey_stamps_the_timeline_order...`. (commit b2e8fdf)
- [x] Step 2: gap fixes. New pure `engine.per_voter(doc)` (VoterChoice: id,
      version, choices; `tally()` now reuses it). Controller drops malformed
      TALLY_SYNC_REQ state vectors (ValueError -> log + drop, no staged reply).
      Tests added. (commit be767a6)
- [x] Step 3: Qt-free presenter + tests (commit 83fd5ef, committed before the
      TODO update was made). Divergences from the plan text: `summarize` in
      place of `survey_summary`, `placeholder_text` in place of `row_text`,
      and `panel_state` was not written — the panel renders directly in
      `qt_tally.TallyPanel`, so that helper proved unnecessary.
- [ ] Step 4: Qt-only UI (TimelineModel, PollsTabModel, TallyPanel,
      TallyCreateDialog, tally_update_queue). In progress: receive-path queue
      plumbing + presenter read helpers + `qt_tally.py` are written but
      uncommitted/untested; see "Current working plan" above.
- [ ] Step 5: katzen.py wiring + chatview.qml placeholders/click.
- [ ] Step 6: test suite + final full unit run.