from __future__ import annotations

import re
from pathlib import Path

CHATVIEW = Path(__file__).resolve().parents[1] / "resources" / "chatview.qml"


def test_the_chat_view_reads_its_colours_from_the_active_system_palette() -> (
    None
):
    text = CHATVIEW.read_text(encoding="utf-8")
    assert re.search(
        r"SystemPalette \{\s*id: sysPalette\s*"
        r"colorGroup: SystemPalette.Active",
        text,
    )
    assert "color: sysPalette.base" in text
    assert text.count("sysPalette.base") >= 3
    assert text.count("sysPalette.text") == 3
    assert "hovered ? sysPalette.alternateBase : sysPalette.base" in text


def test_no_chat_colour_is_a_fixed_light_or_dark_value() -> None:
    text = CHATVIEW.read_text(encoding="utf-8")
    fixed = re.findall(r'color: "(white|black|#[0-9a-fA-F]{3,8})"', text)
    assert fixed == []
