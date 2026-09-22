from katzenqt import katzen


def test_peer_text_is_bounded_and_inert() -> None:
    hostile = ValueError("<b>x</b>" + "A" * 100000 + "\x00\x1b[31m")
    detail = katzen._error_detail(hostile)
    assert detail.startswith("ValueError")
    assert len(detail) <= 256
    assert "<" not in detail and ">" not in detail
    assert all(c.isprintable() or c == " " for c in detail)


def test_the_exception_type_survives() -> None:
    assert katzen._error_detail(RuntimeError("boom")).startswith("RuntimeError")
