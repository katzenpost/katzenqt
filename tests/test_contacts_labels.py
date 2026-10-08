from __future__ import annotations

import os
import uuid

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication  # noqa: E402

from katzenqt.katzen import (  # noqa: E402
    ContactsItem,
    contact_label,
    sibling_labels,
)

from tests.test_katzen_gui_common import boxes, qt_app  # noqa: E402,F401

CAP_A = uuid.UUID("3f2504e0-4f89-11d3-9a0c-0305e82c3301")
CAP_B = uuid.UUID("a1b2c3d4-0000-4000-8000-000000000000")


@pytest.fixture(autouse=True)
def _app(qt_app: "QApplication") -> None:
    return None


def _append(
    parent: ContactsItem, name: str, cap: "uuid.UUID | None",
) -> ContactsItem:
    item = ContactsItem(contact_label(name, cap, sibling_labels(parent)))
    parent.appendRow(item)
    return item


def test_a_repeated_name_is_told_apart_and_the_first_keeps_it() -> None:
    parent = ContactsItem("conversation")
    _append(parent, "alice", CAP_A)
    _append(parent, "alice", CAP_B)
    _append(parent, "bob", CAP_A)
    assert sibling_labels(parent) == [
        "alice", f"alice #{str(CAP_B)[:6]}", "bob",
    ]


def test_three_claims_get_three_labels() -> None:
    parent = ContactsItem("conversation")
    for cap in (CAP_A, CAP_B, uuid.UUID(int=7)):
        _append(parent, "alice", cap)
    assert len(set(sibling_labels(parent))) == 3
    assert "alice" in sibling_labels(parent)


def test_a_row_with_no_cap_keeps_the_bare_name() -> None:
    parent = ContactsItem("conversation")
    _append(parent, "alice", CAP_A)
    assert _append(parent, "alice", None).text() == "alice"
