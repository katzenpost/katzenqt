# TODO

## Next

- [ ] 3. Finish marking a message sent when its ACK arrives after the stream index advanced (persistent.py `mark_sent` stale-ACK path), with stale-ACK test

## Future work

- [ ] SQLite hygiene: drop `echo=True` and shrink `pool_size=1000` on both engines (persistent.py:89-90)
- [ ] `conversation_log_order_lock` is a threading.Lock held across `await`s on the single io loop; two coroutines contending would block the loop (latent same-thread deadlock) — convert to `asyncio.Lock` if it ever needs contention
- [ ] Daemon read ride-out epoch staleness (separate bug): tracked in voucher.py `_read_box` TODO(workaround) and DRAFT.md "Join stall under long waits"