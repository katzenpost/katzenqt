from __future__ import annotations

import xml.etree.ElementTree as ET
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
UI = REPO_ROOT / "ui" / "mixchat.ui"
GENERATED = REPO_ROOT / "src" / "katzenqt" / "ui_mixchat.py"
BUTTON = "invite_contact_toolButton"
TOOLTIP = "Generate a voucher to hand to a member who will induct you"


def _property_of(widget_name: str, property_name: str) -> str:
    for widget in ET.parse(UI).iter("widget"):
        if widget.get("name") != widget_name:
            continue
        for prop in widget.findall("property"):
            if prop.get("name") == property_name:
                value = prop.find("string")
                assert value is not None and value.text is not None
                return value.text
    raise AssertionError(f"{widget_name} has no {property_name}")


def test_the_voucher_button_says_what_it_does() -> None:
    assert _property_of(BUTTON, "toolTip") == TOOLTIP
    assert _property_of(BUTTON, "text").strip() == "Generate voucher"


def test_the_generated_module_carries_the_same_tooltip() -> None:
    line = next(
        one for one in GENERATED.read_text(encoding="utf-8").splitlines()
        if f"{BUTTON}.setToolTip" in one
    )
    assert TOOLTIP in line


def test_no_wording_about_an_invitation_code_survives() -> None:
    for path in (UI, GENERATED):
        assert "invitation code" not in path.read_text(encoding="utf-8")
