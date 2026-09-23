"""The per-stream retry pacing used by the paths that can spin."""
import pytest

from katzenqt import network
from katzenqt.network import PacingBounds, next_ceiling_s, pacing_bounds_for


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


class TestPacingBoundsAreDerivedFromLambdaP:
    def test_no_document_yields_the_static_default(self):
        assert pacing_bounds_for(None) == network.DEFAULT_PACING

    def test_lambda_is_read_as_events_per_millisecond(self):
        bounds = pacing_bounds_for(0.001)
        assert abs(bounds.first_s - 1.0) < 1e-9
        assert 27.0 < bounds.cap_s < 28.0

    def test_a_faster_network_backs_off_less(self):
        slow = pacing_bounds_for(0.001)
        fast = pacing_bounds_for(0.01)
        assert fast.first_s < slow.first_s
        assert fast.cap_s < slow.cap_s

    def test_degenerate_rates_stay_inside_the_static_limits(self):
        for lam in (1e-12, 1e12, float("inf"), float("nan"), 0.0, -1.0, None):
            bounds = pacing_bounds_for(lam)
            assert network._FIRST_LIMITS_S[0] <= bounds.first_s
            assert bounds.cap_s <= network._CAP_LIMITS_S[1]
            assert bounds.first_s <= bounds.cap_s


class TestFollowLambdaP:
    def setup_method(self):
        self.pacer = network.RetryPacer()

    def test_it_reports_only_a_real_change(self):
        assert self.pacer.follow_lambda_p(0.001) is True
        assert self.pacer.follow_lambda_p(0.001) is False
        assert self.pacer.follow_lambda_p(0.01) is True

    def test_the_new_bounds_move_the_ceilings(self):
        self.pacer.follow_lambda_p(0.0005)
        for _ in range(100):
            self.pacer.delay_s("s")
        assert self.pacer.ceilings["s"] == self.pacer.bounds.cap_s
        assert self.pacer.bounds != network.DEFAULT_PACING


class TestABadDocumentDoesNotDiscardGoodBounds:
    @pytest.mark.asyncio
    async def test_a_degenerate_rate_leaves_the_last_good_bounds(self):
        import cbor2
        network._pacer.follow_lambda_p(0.0005)
        good = network._pacer.bounds
        for bad in (0.0, -1.0, float("nan"), float("inf"), "not a number", None):
            await network.on_new_pki_document(
                {"payload": cbor2.dumps({"LambdaP": bad, "Epoch": 1})},
            )
            assert network._pacer.bounds == good
