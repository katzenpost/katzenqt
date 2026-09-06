# TODO

## deckard-wip follow-ups

- [x] **Run the docker integration suite against the live mixnet and fix any
      regressions.** DONE 2026-09-05 against the running mixnet with the patched
      thinclient (git pin): 10/10 passed in 28:09 (file roundtrip, all four
      restart scenarios, tally convergence, all four voucher scenarios);
      command was `KATZENQT_DOCKER_INTEGRATION=1 KATZENQT_KPCLIENTD_PORT=44977
      uv run pytest -x tests/integration/` (no `--no-sync` needed anymore —
      venv is synced to the git-pinned thinclient).

- [x] **Re-test the original wedge scenario end-to-end against the patched
      client.** DONE 2026-09-06.
      The exact pattern (write into a dying kpclientd link; observe the ARQ
      ride out and the message land) is now covered three ways:
      1. **Defensive drain-loop fixes** (committed): `drain_mixwal_write_single`
         probed `get_message_box_index_counter` before the offline try/except,
         so a link drop mid-drain raised into the fire-and-forget `write_task`
         and stranded the stream in `draining_right_now` forever (the
         silently-vanished write); `drain_mixwal2` also awaited that counter
         inside an eagerly-evaluated `logger.debug` f-string, so a single
         offline raise escaped and `drain_mixwal`'s one-shot catch left ALL
         draining dead. Both are offline-safe now, and `write_task` releases
         the stream on exception/cancel (mirrors the resendable `on_error`
         pattern).
      2. **Unit tests** (`test_network_fake.py`): probe-offline is swallowed
         with the MW left for the next pass, and `drain_mixwal2` survives the
         injection and re-sweeps to a successful ACK.
      3. **New integration test** `tests/integration/test_kpclientd_restart.py`
         (serial-only; bounces the SHARED kpclientd): alice READ + bob
         SEND:m0→SLEEP:120→SEND:m1; the container is stopped and held down past
         the SLEEP so m1 is committed while the link is DOWN; after `start`,
         bob's SentLog ACK (`STEP_OK:2:SEND:m1`) and alice's READ land.
         Verified green on the docker mixnet (~6 min); restart suite re-run 4/4
         (one known-flaky `test_read_latency_after_continuous_peer_sends`
         m2-read-timeout on the first run, green in isolation and on the
         re-run). Unit suite 172 passed / 11 skipped; ruff no new findings.
Follow-ups (now separate items below): `network.on_error` re-raise
       noise; and the flaky `test_read_latency_after_continuous_peer_sends`.

- [x] **`network.on_error` re-raises in its done callback (noise).** DONE
      2026-09-06.
      The plan below is what was carried out, with two scope additions found
      mid-flight: (1) `katzen_util.create_task`'s `throw_if_needed` had the
      SAME re-raise pattern and was the actually-loud source during a bounce
      (a thinclient `encrypt_write` task failing on the closed fd) — same
      fix applied (keeps the printed traceback visibility, swallows the
      loop-level callback exception), with a new `tests/test_katzen_util.py`;
      (2) the bounce test's alice-READ window started at process launch, so
      a slow reconnect could burn her budget before m1 existed — she now
      SLEEPs 300s through the bounce and READs after (test-only reliability,
      no product masking). Validation: unit suite 174 passed / 11 skipped;
      ruff no new findings; live bounce run green (6:08) with zero
      "Exception in callback" tracebacks (single intentional `create_task`
      diagnostic print remains).
      `on_error`/`on_error_done` (`src/katzenqt/network.py:759-772`) calls
      `task.result()` and then `raise`s from inside the asyncio
      done-callback. Nothing observes that raise — it only surfaces as
      asyncio's "Exception in callback" traceback (seen in the kpclientd-
      restart integration test's stderr when a resendable plaintext hit the
      dead link during the bounce: `OSError: [Errno 9] Bad file descriptor`).
      The sole usage (`network.py:837`, discard from `__resend_queue` on
      failure so the plaintext is re-swept) needs the callback to fire, not
      the raise. Plan: drop the `raise`, log the swallowed transient at
      debug, keep `task.result()` consumption (avoids the "Task exception
      was never retrieved" warning), fix the stale docstring (`on_error`
      signature is `(task, func, *args, **kwargs)`), and add a unit test
      that installs a stub `loop.set_exception_handler` and asserts the
      handler is NOT invoked when a wrapped task fails. Validate: unit
      suite, ruff, one live bounce run to confirm the role stderr stays
      clean.

- [ ] **Make `test_read_latency_after_continuous_peer_sends` reliable
      (find out why it flakes).** Bob ACKs all 3 sends (courier accepted)
      but alice's read never commits the LAST message (m2) within her read
      deadline, and `STEP_POLL` row_count stays 2 for the whole window.
      Reproduces only in full-restart-suite context under load — green
      every isolated run. Diagnostic-first plan:
      1. **Snapshot on failure**: read-only `sqlite3` dump of each role's
         `<KQT_STATE>.sqlite3` (path built in `persistent.py:125-132`) from
         the test's failure path — ConversationLog count, ReadCapWAL
         `next_index`, leftover read-`MixWAL` rows, SentLog count,
         PlaintextWAL rows. Prints consistently (also on success for
         baseline).
      2. **Reproduce**: loop the restart suite until failures are captured
         (user approved spending hours). Classify:
         (a) leftover read-`MixWAL` for m2 in alice's DB → read-drain
         strand/dispatch bug (read tasks are fire-and-forget without a
         give-up-on-crash callback, and `assert idx_new == idx_old + 1` at
         `network.py:422` can still kill a read task);
         (b) `ReadCapWAL.next_index` advanced past m2 → index-skip race;
         (c) neither → mixnet-side nondelivery, flag evidence to the user
         before masking with a harness workaround.
      3. **Fix + validate**: mirror the write-fix conventions (deterministic
         fake-based unit test first), then full unit suite, restart suite
         x3 green, bounce test re-run green.

- [ ] **thclient 0.0.24 release + pin migration.** The
      `fix-unconditional-replay-keepalive` branch is pushed to GitHub but not
      released: thin_client's `pyproject.toml` is still `0.0.23`, no `0.0.24`
      tag/publish exists. Once cut (needs push/publish access from elsewhere),
      move katzenqt's pin from the branch ref to the tag so `pyproject.toml`
      alone is self-contained and `uv.lock` is just a fast path.

- [ ] **Cross-repo lockstep sync (thin_client-changes.txt, FETCH_NOTES.md).**
      katzenpost `docker/thin_client-changes.txt` and `docker/FETCH_NOTES.md`
      list the pin-sync rules across katzenpost/thin_client/katzenqt; add a
      note describing the 0.0.24 changes (unconditional replay on every
      reconnect; TCP keepalive + `TCP_USER_TIMEOUT` on the daemon socket).
      Also revisit the lockstep refs that still reference the old pair:
      `.github/workflows/test-integration-docker.yml` pins katzenpost
      `d5a6349a` ("lockstep with thin_client 0.0.23 CI") and katzenpost
      docker Makefile `thin_client_ref?=`.

- [ ] **GUI container: confirm/rebuild it runs the patched client.** The
      webtop container mounts this checkout at `/config/katzenqt`. Whether its
      build/venv resolves the git-branch pin (or still installs PyPI 0.0.23)
      has not been checked; verify after the container is next rebuilt so the
      GUI actually benefits from the fix.

## Future work (carried over from prior fix branches)

- [x] **SQLite engine hygiene.** DONE 2026-09-06: both engines now use the
      default QueuePool (size 5); `echo=True` dropped (`persistent.py:89-90`),
      echo-suppression line removed from `katzen.py:1346`; kept
      `headless/__init__.py:152` (also quiets async-pool teardown noise).
      Verified: unit suite 169 passed/10 skipped; docker integration restart
      suite 4/4 passed (no `database is locked` regression from the smaller
      pool).

- [x] **`conversation_log_order_lock` cross-loop deadlock — phase (a): don't
      block loops.** DONE 2026-09-06. The old TODO's "convert to `asyncio.Lock`"
      was wrong as a flat swap: `src/katzenqt/persistent.py:40-52` keys a
      `threading.Lock` per conversation id, and the three sites that hold it
      run on **two different event loops** — the GUI/QtAsyncio loop
      (`src/katzenqt/katzen.py:496`, outbound chat send) and the io thread
      loop (`src/katzenqt/network.py:466`, receive/completion; and
      `src/katzenqt/voucher.py:366`, voucher close). The `threading.Lock` is
      therefore doing genuine cross-loop mutual exclusion, and `asyncio.Lock`
      is not thread-safe and would not serialize across loops. The real bug is
      same-loop contention: a second coroutine on the same loop targeting the
      same conversation froze the loop (the sync `with` blocked in
      `Lock.acquire()` while the first coroutine was awaiting — receive path
      spans awaits at network.py:484, 488, 491, 498, 500; so does
      voucher.py:366). Fix landed: `conversation_log_order_lock` is now a dual
      sync/async context manager — coroutines acquire via `asyncio.to_thread`
      (no loop thread ever blocks), plain threads acquire directly (kept for
      `test_voucher_guard.py:221`'s sync-thread append); all three sites
      switched to `async with`. Regression test
      `tests/test_concurrent_write_orders.py` (8 concurrent send-path appends
      to one conversation on a single loop, inside `asyncio.wait_for`, unique
      `conversation_order`s {0..N-1}): unit suite 170 passed / 10 skipped,
      ruff clean relative to baseline.

- [x] **`conversation_log_order_lock` — phase (b): single-writer funnel.**
      DONE 2026-09-06. The GUI send-path append (`katzen.py`) now hops into
      the io loop via `self.iothread.run_in_io(persistent.append_outbound_chat(
      ...))` instead of appending inline on the QtAsyncio loop, so ALL
      ConversationLog appends run on one loop and aiosqlite sessions + the
      log-order lock are no longer shared across two loops.
      `conversation_log_order_lock` now returns a per-conversation
      `asyncio.Lock` (FIFO, deterministic ordering); the stale "two different
      threads" comment at persistent.py:31-39 is rewritten. `tests/conftest.py`
      resets the lock dict per test (conversation ids restart at 1 after each
      `_fresh_tables` wipe, and asyncio.Lock is loop-affine);
      `test_voucher_guard.py`'s concurrency test was reworked from sync worker
      threads to concurrent coroutines on one loop. Note (still open):
      `send_file` (katzen.py) appends WriteCapWAL/PlaintextWAL on the GUI loop
      WITHOUT the lock and no ConversationLog row — outside lock scope but a
      natural follow-up to funnel through the io loop too. Verified: unit suite
      170 passed / 10 skipped; docker integration restart 4/4 (one
      timing-sensitive latency flake on the first phase-(b) run, green in
      isolation and on the clean re-run).

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

## Cleanup (deferred, low priority)

- [ ] Remove or tidy leftover dev artifacts: the `~/thin_client/.venv` and
      untracked `~/thin_client/uv.lock` created while developing the thinclient
      fix, and `abc_temp2/drive_home` (user cleanup).