"""The voucher QR code must be the one hushphone and the T-Deck show.

hushphone (src/bridge.rs ``qr_image``) encodes the raw code with the Rust
qrcodegen crate: one byte-mode segment, level M, the smallest version, no
ECC boost, a quiet zone of four. HUSHPHONE_MATRIX is what that code
printed for CODE, so katzenqt must draw the same modules.
"""

from __future__ import annotations

import base64
import binascii
import os
import shutil
import subprocess
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest  # noqa: E402
from PySide6.QtGui import QImage  # noqa: E402
from PySide6.QtWidgets import QApplication, QWidget  # noqa: E402

from katzenqt import katzen  # noqa: E402
from katzenqt.voucher import voucher_code  # noqa: E402

from tests.test_katzen_gui_common import qt_app  # noqa: E402,F401

# A compact voucher is 32 bytes, so its code is 44 base64 characters.
CODE = voucher_code(bytes(range(7, 39)))
SCALE = 6
# hushphone's QR for CODE, from qrcodegen 1.8.0 (Rust), '#' dark: version 4.
HUSHPHONE_MATRIX = (
    "#######.##..##.######.....#######",
    "#.....#.#.#.##....#.#####.#.....#",
    "#.###.#..#.#....##..###.#.#.###.#",
    "#.###.#.#.#...#.#...###.#.#.###.#",
    "#.###.#..##.#.#.....#..#..#.###.#",
    "#.....#..#.........####.#.#.....#",
    "#######.#.#.#.#.#.#.#.#.#.#######",
    "........###.#.#..#..#..##........",
    "#.##.###...##...####...#..#..#.##",
    ".#..#.....#...###.#.##.......####",
    "##.#..#.#.#.#.#..#..#..#.#.##.###",
    "..#..#..#..#.####.#.#.......##.#.",
    ".#.#.##.#...#..#..#..#...#.#....#",
    ".##.....#.####..###...#....#.###.",
    "#.#######.##.#.###......#.####...",
    "##.###..###.#...#..####.##.####.#",
    "#.....##..##.#.#.###..#.######..#",
    "..#.##...#.##..#.#####..##.###.#.",
    "###.####..#.##.###..#.#.#.#.##.#.",
    "...#.#.##.##..###...##.###.#...##",
    "##.#..#.####...#..###...#........",
    "###.....##.#...#.##..#........#..",
    "..##..#.#..#..#.##...#.#####...##",
    ".####..#.#.###..#.##...#.#...#.#.",
    "#.###.##.####.##....###.#####..##",
    "........#..#...###......#...###..",
    "#######.###...#.#..#..###.#.#.#..",
    "#.....#.#.####.......##.#...###..",
    "#.###.#...#.###..#.##..#######..#",
    "#.###.#.#..#......###..#......###",
    "#.###.#.#.####...#.#.##.##....#..",
    "#.....#..#...#.#####..#....##...#",
    "#######.###....#..#.##.#.##.#....",
)


def hushphone_parse(code: str) -> bytes | None:
    """pigeonhole_protocols' ``parse_voucher_code``, which hushphone runs
    on what it scans: whitespace dropped, standard base64, 32 bytes."""
    try:
        raw = base64.b64decode("".join(code.split()), validate=True)
    except binascii.Error:
        return None
    return raw if len(raw) == 32 else None


def modules(img: QImage, scale: int) -> list[list[bool]]:
    """The module matrix of a dark-on-light QR image, quiet zone included,
    read from each module's centre pixel."""
    n = img.width() // scale
    return [
        [
            img.pixelColor(
                x * scale + scale // 2, y * scale + scale // 2
            ).lightness()
            < 128
            for x in range(n)
        ]
        for y in range(n)
    ]


def test_matches_hushphones_qr_module_for_module() -> None:
    img = katzen.voucher_qr_image(CODE, scale=SCALE)
    n = len(HUSHPHONE_MATRIX) + 8
    assert img.width() == img.height() == n * SCALE
    quiet = "." * n
    want = (
        [quiet] * 4
        + ["...." + row + "...." for row in HUSHPHONE_MATRIX]
        + [quiet] * 4
    )
    got = [
        "".join("#" if dark else "." for dark in row)
        for row in modules(img, SCALE)
    ]
    assert got == want


def test_every_pixel_is_dark_or_light() -> None:
    img = katzen.voucher_qr_image(CODE, scale=2)
    values = {
        img.pixelColor(x, y).lightness()
        for y in range(img.height())
        for x in range(img.width())
    }
    assert values == {0, 255}


def test_hushphone_accepts_the_code_it_would_scan() -> None:
    assert hushphone_parse(CODE) == bytes(range(7, 39))
    assert hushphone_parse("not a voucher") is None
    assert hushphone_parse(voucher_code(b"short")) is None


@pytest.mark.skipif(
    shutil.which("zbarimg") is None,
    reason="zbarimg is not installed",
)
def test_zbar_reads_the_code_back(tmp_path: Path) -> None:
    png = tmp_path / "voucher.png"
    assert katzen.voucher_qr_image(CODE).save(str(png))
    out = subprocess.run(
        ["zbarimg", "--raw", "-q", str(png)],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    assert out == CODE
    assert hushphone_parse(out) is not None


def test_the_dialog_shows_the_qr_beside_the_text(
    qt_app: QApplication,
) -> None:
    parent = QWidget()
    dialog = katzen.VoucherDialog(parent, CODE)
    assert CODE in dialog.label.text()
    shown = dialog.qr.pixmap().toImage()
    assert shown == katzen.voucher_qr_image(CODE).convertToFormat(
        shown.format(),
    )
