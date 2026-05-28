"""Round-trip and framing invariants for ``models.unserialize``.

The serialise side already chunks a ``SendOperation`` into
``b'F'``/``b'C'``/``b'I'`` framed payloads via
:func:`katzenqt.models.SendOperation.serialize`. The receive side
must walk a chain of chunks and reassemble them into the original
``GroupChatMessage``. This module pins that reassembly against:

* single-box messages (one ``b'F'`` chunk, no continuation),
* multi-box chains (any number of ``b'C'`` chunks ending in
  ``b'F'``, parameterised hypothesis sweep),
* incomplete chains (no ``b'F'`` yet) returning ``None``,
* framing violations raising ``ValueError``.

Indirection (``b'I'``) is the network-layer's concern, not the
data layer's; the data layer raises on encountering one rather
than silently degrading.
"""
from __future__ import annotations

import uuid

import pytest
from hypothesis import given, settings, strategies as st

from katzenqt import models, persistent


def _substream_chunks(db_entries):
    """Return the PlaintextWAL rows that carry the payload chunks,
    in emission order (which is already BACAP order: a chain of
    ``b'C'`` followed by one ``b'F'``). The trailing indirection
    PlaintextWAL is excluded by the ``indirection is None`` filter,
    and the substream's ReadCapWAL is excluded by the isinstance
    filter."""
    return [
        e for e in db_entries
        if isinstance(e, persistent.PlaintextWAL) and e.indirection is None
    ]


def _to_chunks(pwals):
    return [(p.bacap_payload[:1], p.bacap_payload[1:]) for p in pwals]


def test_unserialize_single_box_round_trip():
    """A short message fits in one chunk; the single ``b'F'`` chunk
    must round-trip to the original message."""
    gcm = models.GroupChatMessage(
        version=0, membership_hash=b"X" * 32, text="hello",
    )
    op = models.SendOperation(
        bacap_stream=uuid.uuid4(), messages=[gcm],
    )
    new_caps, db_entries = op.serialize(chunk_size=1530, conversation_id=1)
    assert new_caps == []
    assert len(db_entries) == 1
    payload = db_entries[0].bacap_payload
    assert payload[:1] == b"F"

    recovered = models.unserialize([(payload[:1], payload[1:])])
    assert recovered is not None
    assert recovered.text == "hello"


def test_unserialize_multibox_chain():
    """A long message that spans multiple BACAP boxes must round-
    trip through ``serialize`` + ``unserialize``."""
    text = "A" * 5000
    gcm = models.GroupChatMessage(
        version=0, membership_hash=b"Y" * 32, text=text,
    )
    op = models.SendOperation(
        bacap_stream=uuid.uuid4(), messages=[gcm],
    )
    new_caps, db_entries = op.serialize(chunk_size=200, conversation_id=1)
    assert len(new_caps) == 1, "multi-box send must allocate an ephemeral stream"

    pwals = _substream_chunks(db_entries)
    assert len(pwals) > 1
    chunks = _to_chunks(pwals)
    assert chunks[-1][0] == b"F"
    assert all(c[0] == b"C" for c in chunks[:-1])

    recovered = models.unserialize(chunks)
    assert recovered is not None
    assert recovered.text == text


@settings(max_examples=40, deadline=None)
@given(
    text=st.text(min_size=0, max_size=5000),
    chunk_size=st.integers(min_value=10, max_value=1530),
)
def test_unserialize_round_trip_hypothesis(text, chunk_size):
    """Property: for any text payload and any chunk size in the
    range serialize accepts, ``unserialize`` recovers the original
    text byte-for-byte."""
    gcm = models.GroupChatMessage(
        version=0, membership_hash=b"Z" * 32, text=text,
    )
    op = models.SendOperation(
        bacap_stream=uuid.uuid4(), messages=[gcm],
    )
    _, db_entries = op.serialize(chunk_size=chunk_size, conversation_id=1)
    pwals = _substream_chunks(db_entries)
    chunks = _to_chunks(pwals)

    recovered = models.unserialize(chunks)
    assert recovered is not None
    assert recovered.text == text


def test_unserialize_gap_returns_none():
    """An incomplete chain (no terminating ``b'F'``) returns
    ``None``; appending the missing terminator completes it."""
    cbor = models.GroupChatMessage(
        version=0, membership_hash=b"X" * 32, text="abc",
    ).to_cbor()
    half = len(cbor) // 2
    head, tail = cbor[:half], cbor[half:]

    assert models.unserialize([(b"C", head)]) is None
    recovered = models.unserialize([(b"C", head), (b"F", tail)])
    assert recovered is not None
    assert recovered.text == "abc"


def test_unserialize_empty_chain_returns_none():
    assert models.unserialize([]) is None


def test_unserialize_rejects_unknown_chunk_type():
    with pytest.raises(ValueError, match="unknown chunk type"):
        models.unserialize([(b"X", b"junk")])


def test_unserialize_rejects_indirection_byte():
    """Indirection is the network coalescer's job; the data layer
    must refuse to silently treat it as data."""
    with pytest.raises(ValueError, match="indirection"):
        models.unserialize([(b"I", b"some read cap")])


def test_unserialize_rejects_F_not_at_end():
    with pytest.raises(ValueError, match="'F'"):
        models.unserialize([(b"F", b"a"), (b"C", b"b")])
