from datetime import UTC, datetime, timedelta
from hashlib import sha256

import pytest
from hypothesis import given
from hypothesis import strategies as st

from envoy.domain.ids import OfferHash
from envoy.protocol.limits import ProtocolLimits, RateLimiter, TokenBuckets, stalled

T0 = datetime(2026, 12, 1, tzinfo=UTC)


def h(label: str) -> OfferHash:
    return OfferHash(sha256(label.encode()).digest())


def test_defaults_match_plan_and_take_stricter_bound() -> None:
    limits = ProtocolLimits()
    assert (
        limits.per_sender_per_minute,
        limits.per_pair_per_minute,
        limits.per_session_per_minute,
        limits.sessions_per_pair,
        limits.max_rounds,
        limits.reply_wait,
        limits.stall_rounds,
    ) == (60, 30, 12, 3, 20, timedelta(seconds=60), 3)
    assert limits.rounds(6) == 6
    assert limits.rounds(50) == 20
    assert limits.wait(timedelta(seconds=30)) == timedelta(seconds=30)
    assert limits.wait(timedelta(hours=1)) == timedelta(seconds=60)
    assert limits.wait(None) == timedelta(seconds=60)


@given(st.integers(min_value=1, max_value=120), st.integers(min_value=0, max_value=400))
def test_bucket_never_exceeds_rate_in_a_burst(rate, attempts) -> None:
    buckets = TokenBuckets(rate)
    allowed = sum(buckets.allow("k", T0) for _ in range(attempts))
    assert allowed == min(rate, attempts)


@given(st.lists(st.floats(min_value=0, max_value=5), min_size=1, max_size=200))
def test_bucket_admits_at_most_capacity_plus_refill(gaps) -> None:
    buckets = TokenBuckets(12)
    now = T0
    allowed = 0
    for gap in gaps:
        now += timedelta(seconds=gap)
        allowed += buckets.allow("k", now)
    elapsed = (now - T0).total_seconds()
    assert allowed <= 12 + elapsed * 12 / 60 + 1


def test_bucket_refills_and_keys_are_independent() -> None:
    buckets = TokenBuckets(2)
    assert buckets.allow("a", T0)
    assert buckets.allow("a", T0)
    assert not buckets.allow("a", T0)
    assert buckets.allow("b", T0)
    assert buckets.allow("a", T0 + timedelta(seconds=30))
    with pytest.raises(ValueError, match="positive"):
        TokenBuckets(0)


def test_rate_limiter_applies_session_pair_and_sender() -> None:
    limiter = RateLimiter(ProtocolLimits(per_sender_per_minute=5, per_pair_per_minute=4))
    results = [limiter.allow(1, (9, 1), "s", T0) for _ in range(13)]
    assert sum(results) == 4
    assert not limiter.allow(1, (9, 2), None, T0)
    assert limiter.allow(2, (9, 2), None, T0)
    session_bound = RateLimiter(ProtocolLimits(per_session_per_minute=2))
    assert [session_bound.allow(3, (9, 3), "s", T0) for _ in range(3)] == [True, True, False]
    assert session_bound.allow(3, (9, 3), "t", T0)


def test_stall_detector_flags_repeats_and_lack_of_progress() -> None:
    assert not stalled([], 3)
    assert stalled([(h("a"), 0.2), (h("b"), 0.3), (h("a"), 0.2)], 3)
    progressing = [(h(str(index)), index / 10) for index in range(6)]
    assert not stalled(progressing, 3)
    flat = [(h("a"), 0.5), (h("b"), 0.4), (h("c"), 0.45), (h("d"), 0.5)]
    assert stalled(flat, 3)
    assert not stalled(flat[:3], 3)
