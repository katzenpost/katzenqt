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

State as of 2026-09-11. Session context: recovering from a proxy-sweep storm in a
5-replica katzenpost mixnet while debugging the delivery of Bob's
`jamiroquai.webp` (37300 B) to Alice and Carol. **Delivery of Bob's second
send of jamiroquai.webp has been CONFIRMED to both Alice and Carol** (MD5
`00c8541c15e56ff317c15e755a97427e` verified identical to source). The items
below remain open.

Quick orientation for a new session:

- Repo: `/home/kpdev/katzenqt` (Python client). Sibling Go repo:
  `/home/kpdev/katzenpost` (mixnet / replicas / courier).
- Client code of interest:
  - `src/katzenqt/network.py` — read/write drain loops, `_try_assemble`,
    `drain_mixwal_read_single`, substream handling, `_SUBSTREAM_NAME_PREFIX`
    at `network.py:258`.
  - `src/katzenqt/persistent.py` — MixWAL / PlaintextWAL / ReadCapWAL /
    ConversationPeer / ReceivedPiece models, `get_resendable()` gates
    (`after_id` / `after_stream` around `persistent.py:676-740`).
  - `src/katzenqt/models.py` — `serialize()` at `models.py:82-170`
    (substream creation, `agg_bacap_stream`).
  - `src/katzenqt/katzen.py` — GUI backend; `add_conversation()` at
    `katzen.py:1827-1864`.
  - `src/katzenqt/voucher.py` — `conversation_is_joined()` at `voucher.py:62-73`.
- Mixnet replica code: `/home/kpdev/katzenpost/replica/handlers.go`,
  `/home/kpdev/katzenpost/replica/proxy_request_manager.go`,
  `/home/kpdev/katzenpost/replica/connector.go`.
- Dockerized testnet: `/home/kpdev/katzenpost/docker/mixnet-alpine/`.
  Makefile targets in that dir: if a full containerized-mixnet restart is ever
  needed, pass `base_port=62331` so kpclientd lands on `127.0.0.1:64331`.
- The webtop container (runs the 3 client GUI apps) is `katzenqt_webtop`.
  Client DBs LIVES **inside webtop**, not on the host:
  `/config/.local/share/katzenqt/{a,b,c}.sqlite3` (WAL mode; read with the WAL
  present; owner uid 1001 `abc:abc`). Logs: `/config/katzenqt/{a,b,c}.log`
  (client local time = UTC+2; host/replica logs are UTC).
- kpclientd container: `mixnet-alpine_da39a-kpclientd-1`, runs
  `/mixnet-alpine/kpclientd.alpine -c /mixnet-alpine/client/client.toml` on
  `127.0.0.1:64331`, epoch 2m.
- Clients are relaunched in webtop as user `abc` via
  `/config/katzenqt/start.sh` (`KQT_STATE=a|b|c uv run katzenqt`). To launch
  programmatically, use `podman exec -e DISPLAY=:0 -e
  WAYLAND_DISPLAY=wayland-0 -e XDG_RUNTIME_DIR=/config/.XDG -e
  QT_QPA_PLATFORM=wayland -e DBUS_SESSION_BUS_ADDRESS=... -u abc`; otherwise the
  system tray is unavailable and `window.systray` stays `None`, which crashes
  `conversation_selected` at `katzen.py:1500`
  (`'NoneType' object has no attribute 'has_read_messages'`).

---

## 1. Investigate the katzenpost replica proxy-sweep storm (root cause bug)

Investigate and fix the replica-side proxy storm that has been hammering this
5-replica testnet and was the root cause of the failed first delivery.

### Symptoms / evidence

- Tens of thousands of `proxy sweep budget exhausted` errors in replica logs:
  `replica1=~72,800`, `replica3=~35,000`, `replica4=~25,300`, `replica5=~34,000`
  (paths: `/home/kpdev/katzenpost/docker/mixnet-alpine/replica{1..5}/katzenpost.log`).
- Replicas stuck at 50-85% CPU for days, even today while reads were still
  completing on the second attempt.
- `err=9` (ReplicationFailed) <-> `err=1` (BoxIDNotFound) churn in
  `/home/kpdev/katzenpost/docker/mixnet-alpine/servicenode{1,2,3}/courier/courier.log`.
- Restarting the 5 replicas dropped CPU 50% -> 3-4% for ~2 min, then it crept
  back to 26-85% as the clients' dead-substream read loops re-engaged. Removing
  the dead-substream reads (see item 3) gave steady ~1 crate/min progress with
  only transient stalls.

### Code to inspect

- `/home/kpdev/katzenpost/replica/handlers.go`:
  - `proxySweepBudget` / `proxyAttemptTimeout` around `handlers.go:627-656`;
    `errProxySweepBudgetExhausted` at `handlers.go:662`.
  - The proxy-failover read path (`proxyReadRequest`, "trying the next holder",
    `errorCodeProxyReadTimeout` etc). A recently-rewritten proxy failover
    segment is a strong suspect (self-inflicted probe loops between replicas).
- `/home/kpdev/katzenpost/replica/proxy_request_manager.go` — proxy worker slot
  allocation at `proxy_request_manager.go:125` (`ProxyWorkerCount=0` default?).
- `/home/kpdev/katzenpost/replica/connector.go` — replication dispatch / queue
  for retry at `connector.go:334-388` ("Only dispatched to M/N targets (others
  queued for retry)").
- `/home/kpdev/katzenpost/pigeonhole/errors.go` — replica error codes 0-11.
- Replica `replica.toml` currently uses `ProxyRequestTimeout = 0` and
  `ProxyWorkerCount = 0` (inferred defaults); question whether 0 is actually a
  sane default and whether these need explicit non-zero values.

### Suspects to pursue

1. Infinite/long-lived proxy probe loop when a box genuinely does not exist
   durable, amplified by every client retrying the read (client-side
   `no_retry_on_box_id_not_found=False` in the kpclientd config means rides out
   BoxIDNotFound forever instead of giving up).
2. Replication acknowledged to the client before durability (see item 3) so the
   proxy machinery spins trying to satisfy reads for boxes that will never
   appear.
3. `ProxyWorkerCount` / `ProxyRequestTimeout` defaults of 0 interacting badly
   with the rewritten proxy manager.

---

## 2. Fix: `:substream:` synthetic peers leak into the GUI user list

The synthetic substream peers appear in the contact/user list in the GUI.

### Where

- `src/katzenqt/network.py:258` defines `_SUBSTREAM_NAME_PREFIX = ":substream:"`.
- Substream peers are created with names like `:substream:3:64c9` on I-chunk
  receive (`network.py:755-761`, `active=True`), and retired (`active=False`) on
  F-assembly (`network.py:734`), but are **never deleted** from the DB.
- GUI lists contacts from the DB without filtering: `add_conversation()` at
  `katzen.py:1848-1851` iterates ALL `convo.peers` and thus shows
  `:substream:2:77ca`, `:substream:3:f8f2`, etc. in the user list of Alice's /
  Carol's GUI.
- Contrast: `voucher.py:71` (`conversation_is_joined()`) and `_actions.py`
  correctly exclude `:substream:` names, so the leak is purely a display bug.

### Options

- Filter out `name.startswith(":substream:")` (and/or inactive peers) where the
  user list is rendered (`katzen.py:1848-1851`).
- Optionally actually delete or tombstone retired substream peers instead of
  leaving `active=False` rows forever. (Note item 3 and the DB surgery — stale
  substream peer rows caused real damage; housekeeping here is non-trivial
  because the `active` flag is also used to stop reads.)

---

## 3. Underlying bug that forced DB surgery: dead-substream read amplification (a.k.a. the "replica storm feedback" bug)

Root cause analysis of why Bob's FIRST jamiroquai.webp send failed and why DB
surgery was required to let the SECOND send through. This is the most
important thing to understand before touching item 1.

### Chain of events (observed)

1. On the first send, the client received ACKs from kpclientd for all file
   chunks (e.g. boxes 5519-5525), but boxes ~5526-5543 (18 of 25 chunks) were
   **never durably stored** in the replicas. `BoxIDNotFound` on all replicas
   for that range, permanently. (Blame roller: replication acknowledged before
   durability / replication failures during the storm.)
2. Because each file send gets a **fresh substream** (new
   `agg_bacap_stream` UUID + write/read caps starting at index 0), the lost
   boxes were unrecoverable: there is no way to re-fetch what was never stored,
   and no retry that can resurrect them.
3. The client DB had **no way to forget** the dead substream. `drain_mixwal_read_single`
   (with kpclientd `no_retry_on_box_id_not_found=False`) read loops rode
   `BoxIDNotFound` **forever**, re-issuing reads every few seconds for streams
   that could never yield data. With Bob + Alice + Carol all reading dead
   substreams and ticking the proxy machinery, the replica storm went critical:
   67k+ `proxy sweep budget exhausted`, 5 replicas at ~50% CPU for 3 days.
4. **DB surgery that fixed it** (documented so it can be re-derived, not
   recommended as a permanent fix):
   - Stopped the 3 katzenqt apps + stopped kpclientd.
   - In webtop DBs, for each dead substream peer
     (`conversationpeer.name LIKE ':substream:%'`, rows id=4 named
     `:substream:2:77ca` / `:substream:3:f8f2`): set `active=0`, and deleted the
     matching `mixwal` row whose `bacap_stream` was the dead readcap
     UUID (`538ae279` for Alice, `99614674` for Carol).
   - Copied DBs back into webtop, fixed ownership to `abc:abc`.
   - Backups exist in webtop as `{a,b,c}.sqlite3.bak_disab` /
     `*.sqlite3-wal.bak_disab`.
   - Effect: clients stopped reading the dead substreams; the residual storm
     subsided enough for Bob's second send (fresh substream base
     `3256359971903960859`, I-chunk on main stream at
     `4007537799310947422`) to drain 860->883 in both Alice and Carol and
     deliver the file.

### Schema facts needed to reason about this

- `conversationpeer`: `id/name/active/read_cap_id`; `mixwal`:
  `bacap_stream/current_message_index`; `readcapwal`:
  `id/read_cap/next_index`. `bacap_stream` stored as 32-hex, no dashes.
- `current_message_index` / `next_index` are 104-byte blobs; the first 8 bytes
  are the LE uint64 Pigeonhole box index (BACAP counters).
- Substream write/read caps start at their own index 0.

### Open questions / likely fix surfaces

- What makes a stream "abandonable" once data is known lost? There is no
  current concept of a terminal `Tombstone`/give-up for a *known-lost*
  substream read cap (only the Tombstone replica error for a single box).
- Should the client give up (deactivate the peer + delete the MixWAL) on a dead
  stream after N consecutive `BoxIDNotFound` rides, instead of the current
  infinite `no_retry_on_box_id_not_found=False` ride-out?
- Is the write-side ever told reliably that a chunk was NOT durable? If not,
  the reader is stuck with a read cap pointing at an empty space.

---

## 4. Feature: show substream file-download status in the GUI, with pause/cancel

Currently, when a recipient's main stream yields an `I`-chunk announcing "there
is a file on substream X, download it starting at index 0", the client silently
creates the substream peer and starts draining it. The GUI shows nothing until
the whole file arrives. Feature request (addition, not a bugfix):

- Show an in-UI indication that a file is being downloaded from a substream,
  including its status/progress (pieces received vs total known; see
  `models.serialize()` for chunk counts, `network.py` `_try_assemble`,
  ReceivedPiece rows).
- Give the recipient the ability to **pause** and **cancel** the substream
  download while **continuing to receive/read messages on the main stream**.
  (Currently the read loops are driven by per-MixWAL `drain_mixwal_read_single`
  coroutines keyed on `bacap_stream`, so pause/cancel must target the substream
  MixWAL/WAL entry without touching the main-stream entries — see
  `persistent.MixWAL`, `draining_right_now` set, `readables_to_mixwal()`.)
- Design decisions to surface: how to represent the in-progress file in the QML
  UI model; what "pause" semantically means for a BACAP index (freeze the
  `next_index` cursor and stop the coroutine vs mark the MW and skip in
  `get_resendable()`/`readables_to_mixwal()`); whether cancel should prune
  ReceivedPiece rows + retire the substream peer + delete its MixWAL (mirroring
  the DB surgery in item 3, but done cleanly in-app; possibly also tombstone the
  substream's remaining boxes, which would require the write cap... likely out
  of scope).

---

## 5. Review branch `fix/readarm-drain-race` for relevance — DONE (assessed as unrelated to storm, PR-worthwhile but not a fix for items 1/3)

There is a local (currently unmerged) branch `fix/readarm-drain-race`
(also `fix/readarm-latch-wedge` exists — its parent, but on the audio branch;
still unexamined, ignore for now).

REVIEW OUTCOME (2026-09-11):

- Branch = 2 commits on `b7cc1028` (ancestor of `deckard-dev`, so it applies
  onto our HEAD cleanly; `git merge-tree --write-tree da4276d fix/readarm-drain-race`
  shows a clean merge). Files touched: `src/katzenqt/network.py` (+270, -59
  roughly) and `tests/test_network_fake.py` (+349). No replica/Go code.
  ~70 tests pass on the branch (ran `pytest tests/test_network_fake.py` in the
  existing worktree `/home/kpdev/katzenqt.readarm`).
- What it actually fixes:
  1. `b3f1d10` races the drain RPCs (`get_message_box_index_counter`,
     `start_resending_encrypted_message`, `encrypt_read`, `mark_sent`) against
     `_reconnect_event` / `_epoch_event`, raising `ConnectionLifeInterruptedError`;
     `drain_mixwal_read_single` catches it and `give_up()` (leaves MW for
     idempotent re-send) instead of wedging the stream on an orphaned RPC reply
     after a daemon bounce.
  2. `54ca382` bounds `_wait_for_connection_or_shutdown` with
     `_CONNECTION_IDLE_RETRY_S` and adds an `_ARMING_SWEEP_S` re-arm pass so
     `readables_to_mixwal` / `send_resendable_plaintexts` don't strand forever
     if `__mixnet_connected` is cleared and never re-set on a full kpclientd restart.
- **Relevance vs the session's bugs:**
  - NOT the replica proxy-sweep storm (item 1) — that is Go-side
    (`/home/kpdev/katzenpost`), this branch is Python client-side only.
  - NOT a fix for the dead-substream infinite BoxIDNotFound ride-out (item 3a);
    `no_retry_on_box_id_not_found=False` + "readably alive" peers still ride
    outcome-nonfatal reads forever.
  - NOT the idx-mismatch/duplicate read-cursor race that forced DB surgery
    (item 3b) — none of the "not advancing idx ... already handled?" logic at
    `network.py:645-654` is touched.
  - BUT it does harden the exact failure family we hit operationally
    (client stream wedges / stranded read-arm loops after bounce). It would NOT
    have saved the delivery on its own; the storm was replica-side.
- **Recommendation (drafted, awaiting user go/no-go):** worth a PR to main as a
  general robustness improvement (clean, well-tested, targets daemon-bounce
  wedges), but it does NOT address TODO items 1 or 3. **Do not merge as a
  "fix our storm"** — treat as orthogonal hardening.

> Note: reviewing this branch was task 5; it was deliberately weighted AFTER
> the two bug tasks (1,3) and the feature (4) so review conclusions could be
> weighed against what we now know operationally.