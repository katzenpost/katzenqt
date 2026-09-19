# Debugging katzenqt inside the webtop container

This is a practical guide for **reproducing and diagnosing bugs that surface
during manual GUI testing** in the containerized webtop environment. It is
deliberately general: it is about *how to use webtop to debug*, not about any
particular bug (those belong in `TODO.md`). Update this file as you learn more.

The setup itself is described in `webtop/README.md`; this file adds the
debugging workflows (logs, state inspection, relaunching, and what "normal"
looks like).

## Host lifecycle

- `cd webtop && make start` — build + start the container (podman-compose).
- `cd webtop && make stop` — stop it. `make clean` removes `webtop/uv_cache`.
- Container name: `katzenqt_webtop` (see `webtop/compose.yaml`).
- The web desktop is `http://127.0.0.1:3000`; over SSH forward it
  (`ssh -L 3000:127.0.0.1:3000 <host>`) and open that URL.
- Mounts (relative to `webtop/`): the repo `../` → `/config/katzenqt`
  (read-write), and `./uv_cache` → `/config/.cache/uv`.
  **Gotcha:** the mount is whatever checkout `make start` was run from. Manual
  testing is normally done from the primary checkout; code changes made in a
  separate git worktree are *not* visible until they land in that checkout.

The mixnet itself is a separate set of containers (`podman ps` shows
`mixnet-alpine_*-kpclientd-1`, `*-mix*-1`, …). The clients reach kpclientd
through `host_localhost:64331`, which `compose.yaml` maps to the host loopback.

## Running the three clients

Inside a terminal in the webtop desktop:

```sh
cd /config/katzenqt/webtop && make launch-3
```

`launch-3` starts three clients, one per `KQT_STATE` value `a`, `b`, `c`, using
the container venv `/config/.venv-katzenqt` and
`webtop/thinclient-webtop.toml`. Logs are appended to `{a,b,c}.log`.

Because the log redirect is `>>`, a relaunch accumulates onto the previous run.
For a clean comparison, truncate first (`: > a.log b.log c.log`).

### Relaunching

`launch-3` does **not** kill the previous clients. A second launch while the old
ones are alive will fail the per-state-file instance lock ("already running")
and the new process exits. Kill the old clients first. The live processes look
like:

```
uv run katzenqt
/config/.venv-katzenqt/bin/python3 /config/.venv-katzenqt/bin/katzenqt
```

so the precise kill is:

```sh
pkill -f '/config/.venv-katzenqt/bin/katzenqt'   # the python clients
pkill -f 'uv run katzenqt'                       # their uv wrappers, if left
```

then `make launch-3` again. (`pkill -f katzenqt` also works but is broader; the
patterns above only match the clients.)

## Reading the logs

- Inside the container: `/config/katzenqt/{a,b,c}.log`.
- On the host (same files, via the mount): `~/katzenqt/{a,b,c}.log` for the
  primary checkout, or `<worktree>/{a,b,c}.log` for a worktree.

Tips:

- `rg -n 'Traceback|RuntimeError|ERROR' x.log`.
- `report_exception2 {...}` lines are exceptions raised by QtAsyncio tasks (the
  GUI event loop). The message is a Python repr; the useful part is the
  `'traceback'` string field.
- `[thinclient]` lines are the network client and its timers.
- Lines are interleaved from both event loops, so ordering is not a reliable
  causal signal on its own; use the tracebacks.

## Inspecting client state (read-only)

Each `KQT_STATE` value selects a database file: `KQT_STATE=a` → `a.sqlite3`;
with no `KQT_STATE` set the file is `katzen.sqlite3`. All live at
`/config/.local/share/katzenqt/` inside the container (plus `-wal`/`-shm`).

Query them read-only without disturbing a running client. The container's
`python` (`/lsiopy/bin/python`) has the stdlib, so inject a query with `-c`:

```sh
podman exec katzenqt_webtop python -c "
import sqlite3
con = sqlite3.connect('file:/config/.local/share/katzenqt/c.sqlite3?mode=ro', uri=True)
for row in con.execute('select id, name, first_unread from conversation'):
    print(row)
"
```

Always open with `?mode=ro` (URI) — never write to a live client's state.

Useful tables: `conversation`, `conversationlog`, `conversationpeer` (+
`conversationpeerlink` join table, there is no `conversation_id` column on the
peer), `tallystate` (`survey_id`, `conversation_id`, `doc_state`,
`conversation_order`), `readcapwal`, `writecapwal`, `mixwal`, `plaintextwal`,
`sentlog`, `receivedpiece`, `pendingvoucher`, and `alembic_version`
(`select version_num from alembic_version` = applied schema revision).

Example shape probes:

```sql
select conversation_id, conversation_order, network_status, length(payload)
  from conversationlog order by conversation_id, conversation_order;
select survey_id, conversation_id, conversation_order, length(doc_state)
  from tallystate;
```

For **derived** reads (decoding a `doc_state` CRDT, running the tally engine,
etc.) use the project venv instead of stdlib-only:

```sh
podman exec katzenqt_webtop sh -lc 'cd /config/katzenqt && \
  /config/.venv-katzenqt/bin/python -c "
from katzenqt.tally import engine, sync
print(engine.tally(sync.load_doc(bytes.fromhex(\"...\"))))
"'
```

(Prefer reading the blob with `?mode=ro` as above and passing the bytes in, so
the app import path never touches the live state file.)

## Architecture notes that matter while debugging

- The GUI runs **two event loops**: the QtAsyncio (Qt) loop and an
  `AsyncioThread` "io" loop that owns the thin-client connection and all
  network/drain work. Cross-thread work is handed over with
  `MainWindow.iothread.run_in_io(...)`.
- There are **two SQLAlchemy engines**: the async `aiosqlite` engine
  (`persistent._engine`) used by the io loop, and a sync engine
  (`persistent._engine_sync`) used for Qt-thread reads/writes. They must not be
  crossed: an async engine's pool primitives are bound to the loop that first
  uses them, and the Qt loop opening the async engine can raise
  `RuntimeError: ... is bound to a different event loop`. `persistent.warm_async_engine()`
  is called on the io loop at GUI startup to establish that first connection
  there.
- SQLite runs in WAL mode with a 250 ms `busy_timeout`; readers do not block
  writers. `-wal`/`-shm` files are normal and expected.
