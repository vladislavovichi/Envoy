from datetime import UTC, datetime, timedelta

import pytest
import time_machine

from envoy.ports.clock import FixedClock, SystemClock
from envoy.ports.ids import SequentialIdGenerator, UuidV7Generator
from envoy.ports.rng import SeededRng, SystemRng


def test_fixed_clock_advances_only_on_demand() -> None:
    start = datetime(2026, 1, 1, tzinfo=UTC)
    clock = FixedClock(start)
    assert clock.now() == start
    clock.advance(timedelta(seconds=1))
    assert clock.now() == start + timedelta(seconds=1)
    with pytest.raises(ValueError, match="aware"):
        FixedClock(datetime(2026, 1, 1))


def test_uuid_v7_is_time_ordered() -> None:
    clock = FixedClock(datetime(2026, 1, 1, tzinfo=UTC))
    generator = UuidV7Generator()
    first = generator.new_id()
    clock.advance(timedelta(seconds=1))
    second = generator.new_id()
    assert first.version == second.version == 7
    assert first < second


def test_seeded_rng_is_deterministic() -> None:
    assert SeededRng(5).token_bytes(32) == SeededRng(5).token_bytes(32)
    assert len(SystemRng().token_bytes(32)) == 32


def test_system_clock_and_sequential_ids() -> None:
    with time_machine.travel("2026-01-01T00:00:00Z", tick=False):
        assert SystemClock().now() == datetime(2026, 1, 1, tzinfo=UTC)
    ids = SequentialIdGenerator()
    assert ids.new_id().int == 1
    assert ids.new_id().int == 2
