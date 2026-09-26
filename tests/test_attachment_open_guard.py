"""Unit tests for the pure attachment-open risk classifier.

Kept Qt-free: ``is_risky_attachment_extension`` lives in ``katzen_util`` so the
open-confirmation decision can be exercised without instantiating a QApplication.
"""
from typing import cast

import pytest

from katzenqt.katzen_util import is_risky_attachment_extension


@pytest.mark.parametrize(
    "basename",
    [
        "report.html",
        "page.HTM",
        "vector.svg",
        "doc.pdf",
        "sheet.xml",
        "payload.js",
        "installer.exe",
        "run.desktop",
        "script.sh",
        "weird.name.SVGZ",
    ],
)
def test_risky_extensions_are_flagged(basename):
    assert is_risky_attachment_extension(basename) is True


@pytest.mark.parametrize(
    "basename",
    [
        "photo.jpg",
        "clip.opus",
        "notes.txt",
        "archive.tar.gz",
        "image.png",
        "no_extension",
        "",
        "trailingdot.",
        ".hidden",
    ],
)
def test_safe_or_unknown_extensions_are_not_flagged(basename):
    assert is_risky_attachment_extension(basename) is False


@pytest.mark.parametrize("basename", [5, ["photo.jpg"], b"photo.jpg", None])
def test_a_non_string_basename_is_not_flagged_and_does_not_raise(
    basename: object,
) -> None:
    """A peer names the file, so the field can be any CBOR value."""
    assert is_risky_attachment_extension(cast(str, basename)) is False
