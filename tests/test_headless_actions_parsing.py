"""Unit tests for headless._actions._parse_read_step.

A READ step's payload may carry an optional "...:<deadline_s>" suffix;
splitting it from the left (the original implementation) truncated any
target text that itself contained a colon, and silently discarded an
out-of-range explicit deadline with no warning.
"""
from katzenqt.headless._actions import _parse_read_step


def test_plain_target_with_no_colon():
    assert _parse_read_step("hello", step_idx=0) == ("hello", 360.0)


def test_target_with_valid_deadline_suffix():
    assert _parse_read_step("hello:1800", step_idx=0) == ("hello", 1800.0)


def test_target_containing_a_colon_is_not_truncated():
    # "world" isn't a valid deadline, so the whole thing is the target text.
    assert _parse_read_step("hello:world", step_idx=0) == ("hello:world", 360.0)


def test_out_of_range_deadline_falls_back_and_warns(caplog):
    import logging
    with caplog.at_level(logging.WARNING):
        target, deadline_s = _parse_read_step("hello:10000", step_idx=3)
    assert (target, deadline_s) == ("hello", 360.0)
    assert any("out of range" in r.message for r in caplog.records)


def test_boundary_deadlines_are_accepted():
    assert _parse_read_step("hello:60", step_idx=0) == ("hello", 60.0)
    assert _parse_read_step("hello:7200", step_idx=0) == ("hello", 7200.0)
