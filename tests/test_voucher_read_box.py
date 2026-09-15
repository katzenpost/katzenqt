"""Unit tests for voucher._read_box's retry loop: no existing test drove
it directly, so a regression in its exception handling or round-counting
could pass the full suite silently (a finding from the katzenqt-qa
review). The `fake_thinclient`/`fast_asyncio_sleep` fixtures let these
run many rounds with no real wall-clock delay.
"""
from __future__ import annotations

import asyncio
import logging

import pytest
from katzenpost_thinclient import (
    BoxIDNotFoundError, CourierError, CourierInvalidEpochError,
    DatabaseFailureError, InvalidEpochError, ThinClientOfflineError,
)

from katzenqt import voucher


async def _make_write_read_pair(fake_thinclient):
    """A fresh keypair with nothing written yet."""
    kp = await fake_thinclient.new_keypair(seed=b"\x01" * 32)
    return kp.write_cap, kp.read_cap, kp.first_message_index


async def _write_box(fake_thinclient, write_cap, idx, plaintext):
    wcr = await fake_thinclient.encrypt_write(
        write_cap=write_cap, message_box_index=idx, plaintext=plaintext,
    )
    await fake_thinclient.start_resending_encrypted_message(
        write_cap=write_cap, read_cap=None, message_box_index=None,
        reply_index=None, envelope_descriptor=wcr.envelope_descriptor,
        message_ciphertext=wcr.message_ciphertext,
        envelope_hash=wcr.envelope_hash,
    )


class TestReadBoxRetriesOnTransientErrors:
    @pytest.mark.asyncio
    async def test_retries_past_box_id_not_found_then_succeeds(self, fake_thinclient):
        # Nothing written yet: the first couple of rounds see
        # BoxIDNotFoundError organically (the fake raises it whenever the
        # box isn't in box_store), no injection needed.
        write_cap, read_cap, idx = await _make_write_read_pair(fake_thinclient)
        read_task = asyncio.ensure_future(
            voucher._read_box(fake_thinclient, read_cap, idx, stage="test")
        )
        await asyncio.sleep(0)  # let it see BoxIDNotFoundError at least once
        await _write_box(fake_thinclient, write_cap, idx, b"payload")
        plaintext, _next_idx = await asyncio.wait_for(read_task, timeout=5.0)
        assert plaintext == b"payload"

    @pytest.mark.asyncio
    @pytest.mark.parametrize("exc_cls", [
        InvalidEpochError, CourierInvalidEpochError,
        DatabaseFailureError, CourierError, ThinClientOfflineError,
    ])
    async def test_retries_past_each_transient_error_type_then_succeeds(
        self, fake_thinclient, exc_cls,
    ):
        write_cap, read_cap, idx = await _make_write_read_pair(fake_thinclient)
        # Write the box first: inject_error's queue is consumed by ANY call
        # to start_resending_encrypted_message, including the write below,
        # so the read-side errors must be queued only after it.
        await _write_box(fake_thinclient, write_cap, idx, b"payload")
        for _ in range(3):
            fake_thinclient.inject_error(
                "start_resending_encrypted_message", exc_cls(),
            )

        plaintext, _next_idx = await voucher._read_box(
            fake_thinclient, read_cap, idx, stage="test",
        )
        assert plaintext == b"payload"

    @pytest.mark.asyncio
    async def test_warns_after_sustained_stall(self, fake_thinclient, caplog):
        # Never written: every round hits BoxIDNotFoundError organically.
        # _STALL_WARN_ROUNDS rounds in, _read_box should escalate to a
        # WARNING even though it keeps retrying rather than giving up.
        _write_cap, read_cap, idx = await _make_write_read_pair(fake_thinclient)
        task = asyncio.ensure_future(voucher._read_box(
            fake_thinclient, read_cap, idx, stage="test",
        ))
        with caplog.at_level(logging.WARNING, logger="katzen.voucher"):
            try:
                await asyncio.wait_for(task, timeout=2.0)
            except asyncio.TimeoutError:
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task
        assert any("still not present" in r.message for r in caplog.records)
