"""Pure tests for the vendored message-ordering seam and inline epoch
colouring. Must not load PySide6 (see test_qt_decoupling)."""

from __future__ import annotations

from katzenqt import ordering
from katzenqt.ordering import MessageMeta


def _m(
    order: int,
    peer_id: int,
    arrival: "bytes | None" = None,
    sender: "bytes | None" = None,
) -> MessageMeta:
    return MessageMeta(
        conversation_order=order,
        peer_id=peer_id,
        author=f"peer{peer_id}",
        message_id=f"id{order}",
        arrival_epoch=arrival,
        sender_epoch=sender,
    )


def test_normalize_both_sentinels_and_absent_to_none() -> None:
    assert ordering.normalize_membership_hash(None) is None
    assert ordering.normalize_membership_hash(b"TODO" * 8) is None
    assert ordering.normalize_membership_hash(bytes(32)) is None
    real = b"\x11" * 32
    assert ordering.normalize_membership_hash(real) == real


def test_insertion_order_is_ascending_conversation_order() -> None:
    metas = [_m(2, 1), _m(0, 2), _m(1, 1)]
    assert ordering.get_strategy("insertion").order(metas) == [0, 1, 2]


def test_epoch_anchored_order_bands_by_sender_epoch_keeps_arrival() -> None:
    e0, e1 = b"\xa0" * 32, b"\xb1" * 32
    metas = [_m(0, 1, sender=e0), _m(1, 2, sender=e1),
             _m(2, 1, sender=e0), _m(3, 2, sender=e1)]
    assert ordering.get_strategy("epoch").order(metas) == [0, 2, 1, 3]


def test_unknown_strategy_falls_back_to_insertion() -> None:
    metas = [_m(1, 1), _m(0, 1)]
    assert ordering.get_strategy("nope").order(metas) == [0, 1]
    assert ordering.get_strategy(None).name == "insertion"


def test_active_strategy_reads_env(monkeypatch) -> None:
    monkeypatch.delenv("KQT_ORDERING", raising=False)
    assert ordering.active_strategy().name == "insertion"
    monkeypatch.setenv("KQT_ORDERING", "epoch")
    assert ordering.active_strategy().name == "epoch"


def test_annotate_epochs_marks_boundary_on_change_only() -> None:
    h0, h1 = b"\x01" * 32, b"\x02" * 32
    rows = ordering.annotate_epochs(
        [_m(0, 1, arrival=h0), _m(1, 1, arrival=h0), _m(2, 2, arrival=h1)]
    )
    assert [r.is_boundary for r in rows] == [False, False, True]
    assert rows[0].color == rows[1].color
    assert rows[2].color != rows[1].color


def test_annotate_epochs_recurring_hash_after_leave_recolours_same() -> None:
    h0, h1 = b"\x21" * 32, b"\x22" * 32
    rows = ordering.annotate_epochs(
        [_m(0, 1, arrival=h0), _m(1, 1, arrival=h1), _m(2, 1, arrival=h0)]
    )
    assert [r.is_boundary for r in rows] == [False, True, True]
    assert rows[0].color == rows[2].color
    assert rows[0].color != rows[1].color


def test_epoch_color_is_deterministic_and_shared() -> None:
    h = b"\xab" * 32
    assert ordering.epoch_color(h) == ordering.epoch_color(h)
    assert ordering.epoch_color(None) == "#9e9e9e"
    assert ordering.epoch_color(h).startswith("#")
    assert len(ordering.epoch_color(h)) == 7
