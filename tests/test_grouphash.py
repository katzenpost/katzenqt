from __future__ import annotations

from katzenqt import grouphash

A = bytes(range(32))
B = bytes(range(1, 33))
SENTINEL = b"TODO" * 8


def test_a_real_hash_keeps_its_own_colour() -> None:
    assert grouphash.color_for(A) == grouphash.color_for(A)
    assert grouphash.color_for(A) != grouphash.color_for(B)
    assert grouphash.color_for(A).startswith("#")
    assert len(grouphash.color_for(A)) == 7


def test_a_state_that_is_not_a_hash_is_loud() -> None:
    assert grouphash.color_for(SENTINEL) == grouphash.SUSPECT_COLOR
    assert grouphash.color_for(bytes(32)) == grouphash.SUSPECT_COLOR
    assert grouphash.color_for(b"short") == grouphash.SUSPECT_COLOR


def test_a_state_we_could_not_work_out_is_left_alone() -> None:
    assert grouphash.color_for(None) == grouphash.NO_COLOR


def test_no_rows_have_no_boundaries() -> None:
    assert grouphash.boundaries([]) == []


def test_the_first_row_is_never_a_boundary() -> None:
    assert grouphash.boundaries([A]) == [False]


def test_a_boundary_falls_where_the_state_changes() -> None:
    assert grouphash.boundaries([A, A, B, B, A]) == [
        False, False, True, False, True,
    ]


def test_an_unknown_row_neither_starts_nor_ends_a_run() -> None:
    assert grouphash.boundaries([A, None, A]) == [False, False, False]
    assert grouphash.boundaries([A, None, B]) == [False, False, True]
    assert grouphash.boundaries([None, A]) == [False, False]
