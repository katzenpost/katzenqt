# Why are the KatzenQt docker-integration tests so slow? — a timing report

Scope: the `~/deckard` work area only (katzenqt + `~/deckard/katzenpost` mixnet core,
`~/deckard/thin_client`). Focus: `test_voucher_handshake_then_bidirectional`, which an
instrumented solo run confirmed at **101.3 s**. Suite context: the full 11-test run takes
**~1725 s (~28:45)**, dominated by *intentional* `sleep` waits in three tests — not by the
voucher machinery.

## Method

- Added opt-in per-phase timing to `tests/integration/test_voucher.py`
  (`KQT_INTEGRATION_TIMING=1`): each role subprocess is timed from launch to exit, and any
  `katzen.voucher` round-timing DEBUG lines (`... returned after N.Ns (round r ...)`,
  `... not present yet ...; retrying in 15s`) from its captured stderr are echoed.
- Ran the solo test at `KQT_LOG_LEVEL=DEBUG`, which made every role subprocess emit
  `DEBUG` lines (the shared kpclientd daemon already logs DEBUG to its container journal).
- Correlated the run window (05:34:38Z–05:36:38Z) across:
  - kpclientd container log (`podman logs mixnet-alpine_da39a-kpclientd-1`) — the ARQ
    FSM: `encryptWrite/encryptRead`, `startResendingEncryptedMessage`, `Pigeonhole ARQ
    resend`, `handlePigeonholeARQReply`, `decryptPigeonholeReply`,
  - every replica's `docker/mixnet-alpine/replica{1-5}/katzenpost.log` — box write/read
    legs (proxy vs shard, replication fan-out),
  - `gateway1/katzenpost.log` — gateway wire-session events,
  - live Prometheus metrics (`gateway1:1008`, `mix1:1023`, replicas) for drop counters.

## Solo run: per-phase wall clock

`1 passed in 101.28s`. Every voucher call completed on **round 1** — the Python-side 15 s
retry gap never fired. The time is spent inside the daemon's ARQ ride-out and the
subprocess fixed cost.

| # | Phase | Wall (s) | Daemon-side evidence |
|---|-------|------|------|
| 1 | alice `create-conv` | 1.15 | local provisioning only, no mixnet |
| 2 | bob `create-conv` | 1.16 | local provisioning only |
| 3 | bob `voucher-mint` (write box0) | 6.11 | `encryptWrite` 05:34:53.861 → `Write ACK` 05:34:58.804 |
| 4 | alice `voucher-induct` (read box0, write box1) | 19.55 | box0 read 05:34:59.386→10.870 (11.5 s, round 1); box1 write 05:35:12.115→18.332 |
| 5 | bob `voucher-await` (read box1) | 13.84 | encryptRead 05:35:18.930 → decrypt 05:35:31.536 (12.6 s, round 1) |
| 6 | alice `send` | 15.20 | write transit ~1.0 s (`startResending`→`Write ACK`) + client-side prep |
| 7 | bob `read` | 22.84 | read ride-out ~10.9 s inside daemon (encryptRead 05:35:48.039 → decrypt 58.903) |
| 8 | bob `send` | 8.15 | write transit ~1.3 s + client-side prep |
| 9 | alice `read` | 12.67 | read ride-out ~8.3 s inside daemon |
| . | pytest boot + fixtures | ~3 | implicit in total |

## Per-hop daemon-leg timing table

Each hop timestamp is from the daemon or replica log line during the run. "Mixnet transit"
below = the elapsed from when the daemon handed the packet to the gateway to when the
courier/replica reply came back (single Sphinx round trip through this docker net).

### Box 0 write — bob `voucher-mint` publishes the VoucherPayload

| Leg | Timestamp | Elapsed | Source |
|-----|-----------|---------|--------|
| `encryptWrite` (local crypto → envelope) | 05:34:53.861 | — | kpclientd |
| `startResendingEncryptedMessage` (handoff to gateway) | 05:34:56.877 | 3.02 s (client prep + connect) | kpclientd |
| **Write ACK (courier immediate ack, single round trip)** | 05:34:58.804 | **1.93 s mixnet transit** | kpclientd |
| courier→replica gets the box | 05:34:58.607–58.991 | ~0.4 s after ACK | replica5/replica1 |
| shard replica2 writes box0 + fan-outs replication | 05:35:00.718–01.948 | ~2 s after ACK | replica2 |
| shard replica4 confirms (idempotent match) | 05:35:01.015 | — | replica4 |

Write transit (daemon→gateway→courier→ACK) ≈ **1.9 s**; box durable on shards ≈ +2 s.

### Box 0 read — alice `voucher-induct` reads the VoucherPayload

| Leg | Timestamp | Elapsed | Source |
|-----|-----------|---------|--------|
| `encryptRead` | 05:34:59.386 | — | kpclientd |
| `startResending...` (read, no_retry=true) | 05:35:02.566 | 3.18 s client prep | kpclientd |
| ARQ reply: empty (state 0→1, needs follow-up) | 05:35:03.582 | 1.02 s | kpclientd |
| ARQ resend #1 → empty reply (state 1→1) | 05:35:04.842 → 06.219 | 1.38 s round trip | kpclientd |
| ARQ resend #2 → empty reply (state 1→1) | 05:35:07.486 → 09.355 | 1.87 s round trip | kpclientd |
| ARQ resend #3 → **payload** (state 1→2) | 05:35:09.575 → 10.161 | 0.59 s round trip | kpclientd |
| payload decrypted, returned to client | 05:35:10.870 | — | kpclientd |

**Read = ~11.5 s wall**: the mixnet round trip per resend is only ~0.6–1.9 s; the ~2.4–3.1 s
gaps between replies are the Poisson-gated follow-up *schedule* (echo: follow-up at 03.582→06.219→09.355).
Box0 was already durable (≈05:35:01) but the read's *payload* rounds return empty until
replication is visible to the queried shard/courier path.

### Box 1 write — alice seals the VoucherReply onto bob's stream

| Leg | Timestamp | Elapsed | Source |
|-----|-----------|---------|--------|
| `encryptWrite` | 05:35:12.115 | — | kpclientd |
| `startResending...` (write) | 05:35:16.482 | 4.37 s client prep | kpclientd |
| **Write ACK (single round trip)** | 05:35:18.332 | **1.85 s mixnet transit** | kpclientd |
| shard replica4/proxy writes + fan-out | 05:35:18.739–22.224 | ~4 s after encryptWrite | replica3/4/1 |

### Box 1 read — bob `voucher-await` opens the reply and joins

| Leg | Timestamp | Elapsed | Source |
|-----|-----------|---------|--------|
| `encryptRead` | 05:35:18.930 | — | kpclientd |
| `startResending...` (read, no_retry=true) | 05:35:24.123 | 5.19 s client prep | kpclientd |
| ARQ reply empty (state 0→1) | 05:35:25.688 | 1.57 s | kpclientd |
| ARQ resend #1 → empty reply (state 1→1) | 05:35:26.793 → 29.033 | 2.24 s round trip | kpclientd |
| ARQ resend #2 → **payload** (state 1→2) | 05:35:29.925 → 30.860 | 0.94 s round trip | kpclientd |
| payload decrypted | 05:35:31.536 | — | kpclientd |

**Read = ~12.6 s wall**, ~7.4 s inside daemon (2 empty ride-out rounds + payload round; same
Poisson-gated schedule pattern as box0).

### Gateway wire-session health during the window

- No `Lost connection to gateway "gateway1"` in the run window — the daemon's gateway
  session stayed up for the whole test (good luck: the ~4 min EOF cadence is the concern).
- The known cadence (observed over the live tail, source-audited): the gateway kills the
  daemon's wire session **every ~4 min at an epoch boundary** because
  `core/wire/session.go`'s `DefaultReadTimeout` is **2 min**, re-armed only on a client
  send, while the client only sends once per 2-min epoch (PKI-doc request). Every other
  epoch boundary the read deadline races and trips → EOF, ~2 s reconnect. **This is the one
  real "packet loss" the client can suffer**: a box reply in flight across those ~2 s is
  lost, and kpclientd then waits the ARQ backstop `RoundTripTimeSlop = 20 s`
  (`client/arq.go:19`) before retransmitting — a 20–40 s stall for the affected op.

## Prometheus drop counters (live scrape, OUR stack only)

| Service | Counters | Value |
|---------|----------|-------|
| gateway1 | `dropped_packets_total`, `dropped_reason_total{reason="outgoing_peer_not_connected"}` | **2** (lifetime), all else 0 |
| mix1/mix2/mix3, servicenode1–3 | `dropped_*_packets_total`, `kaetzchen_dropped_*` | **0** |
| replicas | `retry_queue_size` | 0; handshake churn normal (in ~7 ok/1-4 fail, out ~4000 ok/~170 fail) |

**No mix-layer packet loss.** The only "drops" in the whole path are those 2 gateway
packets for a peer momentarily unconnected, plus the epoch-boundary gateway wire-session
EOF described above.

## Cost-driver ranking

1. **Mixnet transit per box op** (dominant): a write ≈ 1.0–1.9 s; a read ≈ 8–13 s composed
   of 2–3 ARQ resend rounds (each ~0.6–2.2 s mixnet round trip) separated by a
   Poisson-gated follow-up schedule (~2.4–3 s apart). Encryption handles are local-crypto fast;
   the thin clients add zero fixed latency (clean synchronous request/reply, `core.rs:1049‑1082`).
2. **Subprocess fixed cost ×9**: every role invocation boots a fresh interpreter + SQLite
   migration + headless connect/teardown (up to ~5 s background drain). `create-conv` is
   ~1.2 s of pure local work; a phase of 3–5 s beyond its transfer math is interpreter/connect
   overhead.
3. **The Python 15 s retry gap never fired** in this run: `_read_box` (voucher.py:41,168‑230)
   is bounded per-round *below* the epoch window; the box-visibility cost is paid by the
   daemon's ride-out instead, so the Python loop stays at round 1 here.
4. **Suite-level** (why 28:45, not the voucher test): intentional waits dominate —
   `time.sleep(250)` in `test_voucher_overlapping_await`, plus the `SLEEP:300`/`SLEEP:120`
   reconnects in the kpclientd-restart/client-reconnect tests.

## Takeaways

- A box **write** is ~2 s end-to-end in this net; a box **read** is ~8–13 s, and the
  second read of the handshake (`voucher-await`) is the long pole at ~12.6 s — both match
  the ~2-min epoch design (read visibility needs current-epoch replication).
- There is no hidden packet loss at the mix layer to hunt. The only interruptible window
  is the **gateway wire-session EOF at epoch boundaries** (2-min `DefaultReadTimeout` vs
  2-min epoch cadence), which can cost 20–40 s via the ARQ 20 s backstop when it strikes.
- If suite time is the target, the leverage is the intentional sleeps
  (`overlapping_await` 250 s, reconnect SLEEPs), not the voucher transfer path.