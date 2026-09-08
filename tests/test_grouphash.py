from __future__ import annotations

from katzenqt import grouphash

A = bytes(range(32))
B = bytes(range(1, 33))
SENTINEL = b"TODO" * 8


def test_no_rows_gives_no_annotations() -> None:
    assert grouphash.annotate([]) == []


def test_the_first_row_is_never_a_boundary() -> None:
    rows = grouphash.annotate([A])
    assert (rows[0].boundary, rows[0].suspect) == (False, False)
    assert rows[0].color == grouphash.color_for(A)


def test_the_same_hash_twice_is_one_group() -> None:
    rows = grouphash.annotate([A, A])
    assert [r.boundary for r in rows] == [False, False]
    assert rows[0].color == rows[1].color


def test_a_different_hash_starts_a_group() -> None:
    rows = grouphash.annotate([A, B])
    assert [r.boundary for r in rows] == [False, True]
    assert rows[0].color != rows[1].color


def test_a_placeholder_hash_is_loud_and_not_inherited() -> None:
    rows = grouphash.annotate([A, SENTINEL, A])
    assert [r.suspect for r in rows] == [False, True, False]
    assert [r.boundary for r in rows] == [False, True, True]
    assert rows[1].color == grouphash.SUSPECT_COLOR
    assert rows[1].color != rows[0].color


def test_a_run_of_placeholders_is_one_group() -> None:
    rows = grouphash.annotate([SENTINEL, bytes(32), b"short"])
    assert [r.suspect for r in rows] == [True, True, True]
    assert [r.boundary for r in rows] == [False, False, False]
    assert {r.color for r in rows} == {grouphash.SUSPECT_COLOR}


def test_a_row_claiming_nothing_is_left_alone() -> None:
    rows = grouphash.annotate([A, None, A])
    assert [r.color for r in rows] == [
        grouphash.color_for(A), grouphash.NO_COLOR, grouphash.color_for(A),
    ]
    assert [r.boundary for r in rows] == [False, False, False]
    assert [r.suspect for r in rows] == [False, False, False]


def test_a_claim_of_nothing_does_not_hide_the_next_change() -> None:
    rows = grouphash.annotate([A, None, B])
    assert [r.boundary for r in rows] == [False, False, True]
