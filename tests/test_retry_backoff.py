"""The per-stream backoff used by the locally-produced failure paths."""
from katzenqt import network


class TestNextBackoff:
    def setup_method(self):
        network._backoff_state.clear()

    def teardown_method(self):
        network._backoff_state.clear()

    def test_first_delay_is_short(self):
        assert network._next_backoff("s") <= network._BACKOFF_FIRST_S

    def test_ceiling_doubles_per_failure(self):
        for _ in range(4):
            network._next_backoff("s")
        assert network._backoff_state["s"] == network._BACKOFF_FIRST_S * 8

    def test_ceiling_is_capped(self):
        for _ in range(100):
            network._next_backoff("s")
        assert network._backoff_state["s"] == network._BACKOFF_CAP_S

    def test_every_delay_stays_within_its_ceiling(self):
        for _ in range(60):
            delay = network._next_backoff("s")
            assert 0.0 <= delay <= network._backoff_state["s"]

    def test_streams_back_off_independently(self):
        for _ in range(5):
            network._next_backoff("busy")
        network._next_backoff("fresh")
        assert network._backoff_state["fresh"] == network._BACKOFF_FIRST_S
        assert network._backoff_state["busy"] > network._BACKOFF_FIRST_S

    def test_success_resets(self):
        for _ in range(5):
            network._next_backoff("s")
        network._reset_backoff("s")
        assert network._next_backoff("s") <= network._BACKOFF_FIRST_S

    def test_delays_are_drawn_not_constant(self):
        seen = {round(network._next_backoff(i), 6) for i in range(50)}
        assert len(seen) > 1
