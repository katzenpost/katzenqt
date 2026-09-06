# TODO

## deckard-wip follow-ups

- [x] **Run the docker integration suite against the live mixnet and fix any
      regressions.** DONE 2026-09-05 against the running mixnet with the patched
      thinclient (git pin): 10/10 passed in 28:09 (file roundtrip, all four
      restart scenarios, tally convergence, all four voucher scenarios);
      command was `KATZENQT_DOCKER_INTEGRATION=1 KATZENQT_KPCLIENTD_PORT=44977
      uv run pytest -x tests/integration/` (no `--no-sync` needed anymore —
      venv is synced to the git-pinned thinclient).

- [ ] **Re-test the original wedge scenario end-to-end against the patched
      client.** This session started from the GUI run where carol's
      doug-introduction write silently vanished (drain hung, no exception).
      The sqlite wedge was proven self-healing headlessly, and the thinclient
      fixes (unconditional in-flight replay + TCP keepalive/user-timeout)
      address the lost-connection hang — but the exact pattern
      (write into a dying kpclientd link; observe the ARQ ride out and the
      message land) has not been replayed against the new client. The closest
      proxy today is the integration restart tests.

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

- [ ] **`conversation_log_order_lock` cross-loop deadlock — fix in two phases.**
      Current state, verified: `src/katzenqt/persistent.py:40-52` keys a
      `threading.Lock` per conversation id, and the three sites that hold it
      run on **two different event loops** — the GUI/QtAsyncio loop
      (`src/katzenqt/katzen.py:496`, outbound chat send) and the io thread
      loop (`src/katzenqt/network.py:466`, receive/completion; and
      `src/katzenqt/voucher.py:366`, voucher close). The `threading.Lock` is
      therefore doing genuine cross-loop mutual exclusion, so the TODO's old
      suggestion "convert to `asyncio.Lock`" is wrong as a flat swap —
      `asyncio.Lock` is not thread-safe and would not serialize across loops.
      The real bug is same-loop contention: a second coroutine on the same loop
      targeting the same conversation blocks the loop thread in the sync
      `with` while the first is awaiting (the receive path holds it across
      awaits at network.py:484, 488, 491, 498, 500 and so does voucher.py:366)
      — a latent but real freeze (rapid double-send; voucher close racing a
      receive completion). Phase (a): keep `threading.Lock` for cross-loop
      atomicity, convert `conversation_log_order_lock` to an async context
      manager that acquires via `asyncio.to_thread` so no loop thread ever
      blocks, switch the 3 sites to `async with`. Phase (b): single-writer —
      move the GUI send-path append (`katzen.py:496-516`) into the io loop via
      `self.iothread.run_in_io(...)`, so ALL ConversationLog appends run on one
      loop; then replace the threading lock with a per-conversation
      `asyncio.Lock` and update the stale "two different threads" comment at
      persistent.py:31-39. Note: `send_file` (katzen.py:616) appends
      WriteCapWAL/PlaintextWAL on the GUI loop WITHOUT the lock and no
      ConversationLog row — outside lock scope but worth revisiting with (b).
      Tests: new `tests/test_concurrent_write_orders.py` — N concurrent
      send-path appends to one conversation on a single loop, inside
      `asyncio.wait_for`, asserting completion, no exceptions, and gathered
      `conversation_order`s exactly {0..N-1} (reproduces today's same-loop
      deadlock as a timeout).

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