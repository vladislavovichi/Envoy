from datetime import UTC, datetime, timedelta

import time_machine

from envoy.app.memory import InMemoryBusinessConnectionRepository
from envoy.app.window import ReplyWindow
from envoy.ports.clock import SystemClock
from envoy.telegram.links import topic_link

START = datetime(2026, 12, 1, 12, tzinfo=UTC)


async def test_window_open_within_24h_closed_after() -> None:
    window = ReplyWindow(InMemoryBusinessConnectionRepository(), SystemClock())
    with time_machine.travel(START, tick=False) as traveller:
        await window.record_incoming("conn", 7)
        assert await window.is_open("conn", 7)
        traveller.shift(timedelta(hours=24))
        assert await window.is_open("conn", 7)
        traveller.shift(timedelta(seconds=1))
        assert not await window.is_open("conn", 7)
        await window.record_incoming("conn", 7)
        assert await window.is_open("conn", 7)


async def test_window_closed_without_any_incoming() -> None:
    window = ReplyWindow(InMemoryBusinessConnectionRepository(), SystemClock())
    assert not await window.is_open("conn", 7)
    await window.record_incoming("conn", 8)
    assert not await window.is_open("conn", 7)
    assert not await window.is_open("other", 8)


def test_topic_link_format() -> None:
    assert topic_link("envoy_bot", 512) == "https://t.me/envoy_bot/512"
    assert topic_link("@envoy_bot", 7) == "https://t.me/envoy_bot/7"
