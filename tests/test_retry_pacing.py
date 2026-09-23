"""The per-stream retry pacing used by the paths that can spin."""
from katzenqt import network
from katzenqt.network import PacingBounds, next_ceiling_s


class TestNextCeiling:
    bounds = PacingBounds(first_s=0.5, cap_s=30.0)

    def test_the_first_failure_uses_the_first_bound(self):
        assert next_ceiling_s(0.0, self.bounds) == 0.5

    def test_the_ceiling_doubles(self):
        assert next_ceiling_s(2.0, self.bounds) == 4.0

    def test_the_ceiling_stops_at_the_cap(self):
        assert next_ceiling_s(20.0, self.bounds) == 30.0

    def test_it_is_pure(self):
        assert next_ceiling_s(2.0, self.bounds) == next_ceiling_s(2.0, self.bounds)


class TestRetryPacer:
    def setup_method(self):
        self.pacer = network.RetryPacer()

    def test_the_first_delay_is_within_the_first_bound(self):
        assert self.pacer.delay_s("s") <= self.pacer.bounds.first_s

    def test_the_ceilings_climb_then_stop_at_the_cap(self):
        for _ in range(100):
            self.pacer.delay_s("s")
        assert self.pacer.ceilings["s"] == self.pacer.bounds.cap_s

    def test_every_delay_stays_within_its_ceiling(self):
        for _ in range(60):
            delay = self.pacer.delay_s("s")
            assert 0.0 <= delay <= self.pacer.ceilings["s"]

    def test_streams_back_off_independently(self):
        for _ in range(5):
            self.pacer.delay_s("busy")
        self.pacer.delay_s("fresh")
        assert self.pacer.ceilings["fresh"] == self.pacer.bounds.first_s
        assert self.pacer.ceilings["busy"] > self.pacer.bounds.first_s

    def test_a_reset_clears_one_stream(self):
        for _ in range(5):
            self.pacer.delay_s("s")
        self.pacer.reset("s")
        assert self.pacer.delay_s("s") <= self.pacer.bounds.first_s

    def test_a_reset_of_an_unknown_stream_is_harmless(self):
        self.pacer.reset("never seen")

    def test_the_delays_are_drawn_not_constant(self):
        seen = {round(self.pacer.delay_s(i), 6) for i in range(50)}
        assert len(seen) > 1

    def test_pacers_do_not_share_state(self):
        other = network.RetryPacer()
        for _ in range(5):
            self.pacer.delay_s("s")
        assert "s" not in other.ceilings
