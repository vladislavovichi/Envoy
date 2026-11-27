from collections.abc import Hashable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta

from envoy.domain.ids import OfferHash


@dataclass(frozen=True)
class ProtocolLimits:
    per_sender_per_minute: int = 60
    per_pair_per_minute: int = 30
    per_session_per_minute: int = 12
    sessions_per_pair: int = 3
    max_rounds: int = 20
    reply_wait: timedelta = timedelta(seconds=60)
    stall_rounds: int = 3
    nonce_window: timedelta = timedelta(minutes=10)
    max_clock_skew: timedelta = timedelta(minutes=2)
    retry_interval: timedelta = timedelta(seconds=5)

    def rounds(self, mandate_rounds: int) -> int:
        return min(self.max_rounds, mandate_rounds)

    def wait(self, mandate_wait: timedelta | None) -> timedelta:
        return self.reply_wait if mandate_wait is None else min(self.reply_wait, mandate_wait)


class TokenBuckets:
    def __init__(self, per_minute: int) -> None:
        if per_minute <= 0:
            raise ValueError("Rate must be positive")
        self._capacity = float(per_minute)
        self._rate = per_minute / 60.0
        self._buckets: dict[Hashable, tuple[float, datetime]] = {}

    def allow(self, key: Hashable, now: datetime) -> bool:
        tokens, last = self._buckets.get(key, (self._capacity, now))
        elapsed = max((now - last).total_seconds(), 0.0)
        tokens = min(self._capacity, tokens + elapsed * self._rate)
        if tokens < 1.0:
            self._buckets[key] = (tokens, now)
            return False
        self._buckets[key] = (tokens - 1.0, now)
        return True


class RateLimiter:
    def __init__(self, limits: ProtocolLimits) -> None:
        self._sender = TokenBuckets(limits.per_sender_per_minute)
        self._pair = TokenBuckets(limits.per_pair_per_minute)
        self._session = TokenBuckets(limits.per_session_per_minute)

    def allow(self, sender: int, pair: Hashable, session: Hashable | None, now: datetime) -> bool:
        if not self._sender.allow(sender, now):
            return False
        if not self._pair.allow(pair, now):
            return False
        return session is None or self._session.allow(session, now)


def stalled(history: Sequence[tuple[OfferHash, float]], window: int) -> bool:
    if not history:
        return False
    last, _ = history[-1]
    if any(previous == last for previous, _ in history[:-1]):
        return True
    if len(history) <= window:
        return False
    baseline = history[-window - 1][1]
    return max(utility for _, utility in history[-window:]) <= baseline


def contiguous(received: int, seq: int) -> bool:
    return seq == received + 1
