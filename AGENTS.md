# Notes for agents working on katzenqt

Orientation for an AI/automation agent (or a new human) working in this repo.
It covers the load-bearing architecture and the repo's working conventions —
the things that cost the most time to rediscover. It is not a feature spec; see
`HACKING.md`, `BUILD.md`, `docs/`, and the `webtop/AGENTS.md` (webtop-based
manual testing) for those.

## What this is

`katzenqt` is a PySide6 (Qt6) + QML desktop client for Katzenpost mixnet group
chat. Python package is `src/katzenqt/`. The QML chat view is
`resources/chatview.qml`. The app entrypoint is `katzenqt:cli`
(`src/katzenqt/katzen.py`); there is also a headless CLI
(`katzenqt-headless`, `src/katzenqt/headless/_actions.py`) used by integration
tests.

## Key modules

- `src/katzenqt/network.py` — read/write drain loops, `_try_assemble`,
  `drain_mixwal_read_single`, `drain_mixwal_write_single`, substream handling,
  the `pause_peer_reads` / `resume_peer_reads` primitives, and
  `substream_progress_queue`.
- `src/katzenqt/persistent.py` — MixWAL / PlaintextWAL / ReadCapWAL /
  WriteCapWAL / ConversationPeer / ReceivedPiece models, and
  `PlaintextWAL.find_resendable()` (the `after_id` / `after_stream` gates).
- `src/katzenqt/models.py` — `SendOperation.serialize()`: chunk splitting,
  substream (`agg_bacap_stream`) creation, the indirection ReadCapWAL and the
  I-chunk PlaintextWAL.
- `src/katzenqt/katzen.py` — GUI backend: `MainWindow`, the `@async_cb`
  actions, the supervised listeners, `add_conversation()`.
- `src/katzenqt/qt_models.py` — `ConversationUIState`, `DownloadsModel`, and the
  chat/transfer model roles.
- `src/katzenqt/voucher.py` — the contact-voucher handshake
  (`conversation_is_joined()`, `mint_and_publish`, `derive_read_and_induct`).
- `src/katzenqt/conversation_handlers.py` — inbound message routing.
- `src/katzenqt/tally/` — the tally engine, sync, and controller.

## The two-event-loop, two-engine architecture (most important)

This is the single biggest source of subtle bugs. Get it right.

- **Two event loops.**
  - The **Qt loop** (`QtAsyncio.run(...)`, `katzen.py`'s `cli`) runs the GUI,
    QML, and the `MainWindow` methods decorated with `@async_cb` (scheduled via
    `create_task`).
  - A separate **io loop** (`AsyncioThread`, `katzen.py`) owns the thin-client
    connection and all network/drain work.
  - Cross-thread work is handed to the io loop with
    `MainWindow.iothread.run_in_io(...)` (a `run_coroutine_threadsafe` +
    `await`). Anything that touches the network or opens an **async** DB session
    must run there.
- **Two SQLAlchemy engines** over the same SQLite file
  (`persistent.py`):
  - `persistent._engine` — **async** (`sqlite+aiosqlite`), used by the io loop.
  - `persistent._engine_sync` — **sync**, used for Qt-thread reads/writes via
    `persistent.Session(persistent._engine_sync)`.
  - **Never cross them.** An async engine's pool primitives bind to the event
    loop that first uses them; the Qt loop opening the async engine used to
    raise `RuntimeError: ... is bound to a different event loop`. Fix in place:
    `persistent.warm_async_engine()` is awaited on the io loop at GUI startup to
    establish the first async connection there, and Qt-thread code uses only the
    sync engine. When adding Qt-thread DB access, use the sync engine. When
    adding io-loop code, use `persistent.asession()`.
  - SQLite runs WAL with a 250 ms `busy_timeout`; readers don't block writers;
    `-wal`/`-shm` files are normal.
- **Qt model mutation happens on the Qt thread**, driven by notifications from
  the io loop (queues in `network.py`). The GUI's `receive_msg_listener` etc.
  drain those and update models. Do not mutate Qt models from the io loop.

## QtAsyncio tasks, dialogs, and re-entrancy

Blocking modals (`dialog.exec()`, `QInputDialog.getText`, `QMessageBox.*`,
`QMenu.exec`) must never run inside an `@async_cb` task: they spin a nested Qt
event loop that re-enters another task's `QtAsyncio` `_step`, corrupts the task
bookkeeping, freezes the supervised listeners, and can silently swallow work
being done by the interrupted task.

- Never spin a nested Qt event loop inside a QtAsyncio task.
- Blocking dialogs and menus are safe only from plain sync slots (top-level
  event dispatch, no task mid-step) or via `QTimer.singleShot`.
- Inside a task, go through the non-blocking helpers `_dialog_finished(dialog)`
  / `_menu_chosen(menu, global_pos)` (`katzen.py`).
- Long-lived listeners are supervised and restarted; `async_cb` logs failures
  rather than letting them vanish.

## Conversation log, ordering, and the message types

- **`ConversationLog`** (`persistent.py`) is the per-conversation message log.
  Its `conversation_order` is assigned as a live `COUNT(*)` subquery at commit
  (`persistent.next_conversation_order`) **under
  `persistent.conversation_log_order_lock(conversation_id)`** — hold that lock
  around every append that assigns an order. Order is distinct per conversation
  via a unique constraint; `ConversationLogModel` maps `index_row ==
  conversation_order` 1:1.
- **Message kinds** are routed in `conversation_handlers.py::dispatch`, by
  `models.GroupChatTypeEnum`: ordinary chat (`TEXT`, `FILE_UPLOAD`, `WHO`,
  `REPLY_WHO`), `INTRODUCTION` (member added announcement), and the tally
  family (`TALLY_CREATE/VOTE/CLOSE/SYNC_REQ/SYNC_RESP`). **Every kind, including
  tally messages, becomes a `ConversationLog` row** (rendered specially; tally
  rows are decoded in `qt_models.py`). `dispatch` returns
  `(convlog_added, signal_send, peer_added, tally_added)`; the receive path
  uses these to fire the relevant notification queues post-commit.
- **Voter/member identity** is `hashlib.blake2b(read_cap[:32], digest_size=16)`
  (`tally/controller.voter_id_from_read_cap`). It hashes only the 32-byte
  public-key prefix of the BACAP capability, because the trailing 104-byte index
  suffix varies per copy (pre-mutation vs. salt-mutated vs. future-only caps) —
  see "capability" below. Membership hashing (`models.canonical_membership_hash`)
  likewise keys on `cap[:32]`.

## BACAP / MixWAL / substream facts

Re-derivable from `persistent.py`, but easy to get wrong:

- `mixwal.bacap_stream` (and the other `bacap_stream` UUID columns) is stored by
  SQLite as 32 hex chars, no dashes.
- `current_message_index` / `next_index` are 104-byte blobs whose first 8 bytes
  are the little-endian uint64 Pigeonhole box index (BACAP counters).
- A substream is its own BACAP stream: its write/read caps start at their own
  index 0, independently of the parent stream. An oversized send becomes
  C-chunks plus a final F-chunk on a fresh `agg_bacap_stream`; the main-stream
  I-chunk announces it and carries the chunk count in its extended 140-byte
  form, persisted as `ReadCapWAL.substream_total_chunks`.
- A retired substream peer (`active=0`, ReceivedPiece rows pruned, no MixWAL
  row) is the **normal terminal state** after F-assembly, not a stall. The
  dead-substream fail-fast (first `BoxIDNotFound`/`Tombstone`) looks similar
  but WARNING-logs and keeps the pieces.
- `pause_peer_reads` / `resume_peer_reads` freeze and re-arm a single BACAP
  read stream from its saved `next_index`.

## Notifications from the io loop to the GUI

`network.py` has module-level `asyncio.Queue`s the receive path fills
**post-commit**: `conversation_update_queue` (a log row was added; bool is
"redraw only"), `peer_added_queue`, `tally_update_queue` (a tally event landed),
`substream_progress_queue`. A `MainWindow._supervised_listener(...)` task
drains each on the Qt loop. A `report_exception2` handler logs
unhandled QtAsyncio task exceptions.

- The **redraw/repaint contract**: any writer that appends a `ConversationLog`
  row should wake `conversation_update_queue` so the view refreshes. The model's
  row count, however, is **derived from the log via `refresh_row_count()`**
  (cached `COUNT(*)`), so a missed notification self-heals on the next one
  rather than permanently desyncing the view. `ConversationLogModel` tracks two
  counts: `_row_count` (DB truth, what `rowCount()` returns) and `_view_count`
  (rows Qt has been told about, which drives insert/reset transitions) — keep
  them distinct.

## Qt item models — hard-won rules

- **`ConversationLogModel.index()` must return a standard index.** It used to
  pass a custom `id=` and `@lru_cache` the returned `QModelIndex`; QML's
  `TreeView` adapts the model through `QQmlTreeModelToTableModel`, which stores
  `QPersistentModelIndex`es, and the custom identity desynced it and segfaulted
  in `showModelChildItems`. Use `createIndex(row, column)` with no `id`, bounds
  it, and don't cache `QModelIndex` values.
- **Drive model transitions from what Qt has been told**, not from a raw DB
  count; emit an insert only for the newly-appended range, reset on shrink,
  repaint in place otherwise. A mismatched insert range crashes the view.
- `data()` opens a sync-engine `Session` and runs one indexed SELECT per
  uncached `(index, role)`, wrapped in an lru cache; `_clear_data_caches()`
  drops those caches on a count change. There is a dated TODO in
  `qt_models.py` about reducing this DB chatter.
- Offscreen Qt tests set `QT_QPA_PLATFORM=offscreen`, create a **`QApplication`**
  (not `QGuiApplication`; only one app instance may exist per process, and
  widget tests need `QApplication`, so all Qt test modules agree on it), and
  live under `tests/test_qt_tally.py`, `tests/test_conversation_log_model.py`,
  etc. `ConversationLogModel.index()/rowCount()` require the `parent` argument.

## Capabilities / vouchers (contact induction)

Joining is via the Contact Voucher protocol (`voucher.py`, spec at
`spec/contact-vouchers.md`). The joiner mints a voucher, the inductor reads it
and replies with read caps. Key facts that have caused bugs:

- A member's read capability is `public_key(32) || index(104)` (136 bytes). The
  joiner's **own** copy and the copy the group holds differ in the index suffix
  until the handshake mutates the stream; `voucher.await_and_open` sets the
  conversation `WriteCapWAL.write_cap` to the salt-mutated cap and must also
  update the own peer's `ReadCapWAL.read_cap` to `mutated[32:]`. This is why
  identity keys on `[:32]`.
- The wire mess. `models.canonical_membership_hash` is a brand-new field; no
  clients depend on the old derivation.

## Working conventions in this repo

- **Tests**: `uv run pytest` (not `make test-uv`); `uv run pytest --no-cov`
  skips coverage for a quicker unit suite. The root `conftest.py` points
  `KQT_STATE` at a temp DB before `persistent` is imported; `tests/conftest.py`
  wipes tables and resets `network` module state per test (autouse). Docker
  integration tests live under `tests/integration`, are gated by
  `KATZENQT_DOCKER_INTEGRATION=1`, and need the docker mixnet up. Qt tests are
  offscreen. `make test` / `make test-uv` exist but `uv run pytest` is the
  documented shortcut. Host tests use the repo-local `.venv` (uv, CPython 3.13,
  editable katzenqt + `~/thin_client`).
- **Comments and docstrings** describe current behavior only. Root-cause
  narratives, "used to ..." notes, and tuning/justification history belong in
  **commit messages**, not in the code. Never reference `TODO item N` (or the
  plan's item numbers) in code, docstrings, tests, or migration headers — the
  numbers are bookkeeping that goes stale. Write for a reader who sees only the
  current code: current behavior, current invariants, current recovery
  contracts. In tests, the docstring states the behavior under test; where
  several tests share a caveat, state it once concisely rather than
  cross-referencing another test. Loose `TODO:` comments with no plan number are
  fine where they mark a genuine open question, but keep them specific and
  current.
- **Generated files**: `src/katzenqt/ui_mixchat.py` is generated by
  `pyside6-uic` from `ui/mixchat.ui` (`make code-generator`). Never hand-edit the
  generated Python; edit the `.ui` and regenerate. Same for
  `resources_rc.py` from the `.qrc`.
- **TODO.md** (when present) is a living plan committed to git; keep it updated
  with separate `update TODO.md: ...` commits, not mixed into code commits.
