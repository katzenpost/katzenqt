from __future__ import annotations

from katzenqt.network import RetryPacer


def test_the_delay_sits_in_the_upper_half_of_the_ceiling() -> None:
    pacer = RetryPacer()
    for _ in range(200):
        delay = pacer.delay_s("stream")
        ceiling = pacer.ceilings["stream"]
        assert ceiling / 2.0 <= delay <= ceiling


def test_two_streams_do_not_share_a_delay() -> None:
    pacer = RetryPacer()
    seen = {pacer.delay_s(f"stream-{n}") for n in range(40)}
    assert len(seen) > 1


def test_the_delay_is_not_drawn_from_the_shared_generator() -> None:
    import katzenqt.network as network

    assert not hasattr(network, "random")
