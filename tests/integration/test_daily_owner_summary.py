import json
from datetime import timedelta

import pytest

from envoy.app.daily_summary import DailySummary
from envoy.domain.telegram import BusinessMessageUpdate
from envoy.llm.translator import IntentWire
from tests.support.harness import CONNECTION, OWNER_USER, build_harness, message_is
from tests.support.sessions import T0
from tests.support.widgets import SUMMARY

EVENING = T0.replace(hour=20)


class SimulatedCrash(BaseException):
    pass


async def owner_with_sessions(count: int, **options):
    options.setdefault("owner_view", "widget")
    harness = build_harness(**options)
    harness.llm.add(message_is("привет"), IntentWire(kind="unclear"))
    await harness.connect()
    await harness.activate()
    for index in range(count):
        chat = 10 + index
        for user, text in ((chat, "привет"), (OWNER_USER, "/envoy")):
            identity = harness.update_id()
            await harness.feed(
                BusinessMessageUpdate(
                    identity, CONNECTION, chat, user, message_id=identity, text=text
                )
            )
        harness.clock.advance(timedelta(minutes=1))
    return harness


def restarted(harness) -> DailySummary:
    pipeline = harness.pipeline
    return DailySummary(
        pipeline.steps,
        pipeline.owner_view,
        pipeline.stores.owner_views,
        harness.transport,
        harness.transport,
        "UTC",
    )


async def run_at(harness, moment, summary: DailySummary | None = None) -> None:
    harness.clock.advance(moment - harness.clock.now())
    await (summary or restarted(harness)).run_due(harness.clock.now())


def summaries(harness) -> list:
    return harness.widgets_of(SUMMARY)


def text_lines(harness) -> list[str]:
    return [
        line
        for record in harness.transport.sent
        if record.business_connection_id is None
        for line in record.message.text.split("\n")
        if line.startswith("• Чат")
    ]


async def test_nothing_before_evening_or_without_active_sessions() -> None:
    harness = await owner_with_sessions(2)
    await run_at(harness, EVENING - timedelta(minutes=1))
    assert summaries(harness) == []
    empty = build_harness(owner_view="widget")
    await run_at(empty, EVENING + timedelta(hours=1))
    assert empty.transport.widgets == {}
    assert empty.transport.sent == []


async def test_two_runs_with_restart_deliver_one_summary_per_owner_and_day() -> None:
    harness = await owner_with_sessions(2)
    first = restarted(harness)
    await run_at(harness, EVENING, first)
    await run_at(harness, EVENING + timedelta(minutes=5), first)
    await run_at(harness, EVENING + timedelta(minutes=10))
    assert len(summaries(harness)) == 1
    widget = summaries(harness)[0]
    assert widget.allow_queries is True
    assert widget.caption.startswith("Сводка Envoy за 01.12.2026, активных сделок: 2")
    topic = await harness.pipeline.stores.owner_views.summary_topic(
        (await harness.session()).owner_id
    )
    assert widget.thread_id == topic is not None
    await run_at(harness, EVENING + timedelta(days=1))
    assert len(summaries(harness)) == 2


async def test_twelve_sessions_fill_widget_prefix_and_rest_as_text() -> None:
    harness = await owner_with_sessions(12)
    await run_at(harness, EVENING)
    widget = summaries(harness)[0]
    chat_data = widget.state
    state = json.loads(chat_data)
    widget_rows = state["rows"]
    assert len(widget_rows) <= 10
    assert len(chat_data.encode("utf-8")) <= 1024
    assert len(widget.caption) <= 1024
    assert state["total"] == 12
    assert state["links"] == "request"
    rest = text_lines(harness)
    total_rows_rendered = len(widget_rows) + len(rest)
    assert total_rows_rendered == 12
    labels = [row["label"] for row in widget_rows] + [line.split(" — ")[0][2:] for line in rest]
    assert labels == [f"Чат {10 + index}" for index in range(12)]
    assert all(row["url"].startswith("https://t.me/envoy_bot/") for row in widget_rows)


async def test_pending_escalation_sorts_first() -> None:
    from tests.support.widgets import CONFIRMING

    harness = build_harness(owner_view="widget")
    harness.llm.add(message_is("привет"), IntentWire(kind="unclear"))
    harness.llm.add(message_is("ок"), IntentWire(kind="accept"))
    await harness.connect()
    await harness.activate(CONFIRMING)
    for chat in (10, 11):
        for user, text in ((chat, "привет"), (OWNER_USER, "/envoy")):
            identity = harness.update_id()
            await harness.feed(
                BusinessMessageUpdate(
                    identity, CONNECTION, chat, user, message_id=identity, text=text
                )
            )
    identity = harness.update_id()
    await harness.feed(
        BusinessMessageUpdate(identity, CONNECTION, 11, 11, message_id=identity, text="ок")
    )
    pipeline = harness.pipeline
    noon = DailySummary(
        pipeline.steps,
        pipeline.owner_view,
        pipeline.stores.owner_views,
        harness.transport,
        harness.transport,
        "UTC",
        hour=12,
    )
    await noon.run_due(harness.clock.now())
    rows = json.loads(summaries(harness)[0].state)["rows"]
    assert [(row["label"], row["status"]) for row in rows] == [
        ("Чат 11", "owner"),
        ("Чат 10", "counterpart"),
    ]


async def test_link_request_sends_one_link_and_rejects_invalid_requests() -> None:
    harness = await owner_with_sessions(2)
    await run_at(harness, EVENING)
    widget = summaries(harness)[0]
    message_id = widget.message.message_id
    url = json.loads(widget.state)["rows"][1]["url"]
    before = len(harness.transport.sent)
    update = await harness.widget_query("l:1:1", message_id)
    await harness.feed(update)
    sent = harness.transport.sent[before:]
    assert [record.message.text for record in sent] == [f"Чат 11: {url}"]
    assert sent[0].message.chat_id == OWNER_USER
    for data in ("l:0:1", "l:1:2", "l:1:01", "l:2:0"):
        await harness.widget_query(data, message_id)
    await harness.widget_query("l:1:0", message_id + 50)
    await harness.widget_query("l:1:0", message_id, user_id=77)
    assert len(harness.transport.sent) == before + 1
    assert harness.widget_answers() == ["ok", "stale", "no", "no", "stale", "no", "no"]


async def test_without_topics_summary_goes_to_main_chat_and_rows_have_no_link() -> None:
    harness = await owner_with_sessions(1, topics=False)
    await run_at(harness, EVENING)
    widget = summaries(harness)[0]
    assert widget.thread_id is None
    assert json.loads(widget.state)["rows"][0]["url"] is None
    await harness.widget_query("l:1:0", widget.message.message_id)
    assert harness.widget_answers() == ["no"]


async def test_direct_link_mode_is_passed_to_widget() -> None:
    harness = await owner_with_sessions(1, links="direct")
    await run_at(harness, EVENING)
    assert json.loads(summaries(harness)[0].state)["links"] == "direct"


@pytest.mark.parametrize("owner_view", ["text", "widget"])
async def test_text_path_renders_every_row(owner_view) -> None:
    harness = await owner_with_sessions(12, owner_view=owner_view)
    harness.transport.widgets_available = False
    await run_at(harness, EVENING)
    assert summaries(harness) == []
    lines = text_lines(harness)
    assert len(lines) == 12
    assert all("https://t.me/envoy_bot/" in line for line in lines)


async def test_restart_after_widget_before_mark_does_not_resend_widget() -> None:
    harness = await owner_with_sessions(12)
    store = harness.pipeline.stores.owner_views
    original = store.mark_summary_delivered

    async def crash(*args):
        raise SimulatedCrash

    store.mark_summary_delivered = crash
    with pytest.raises(SimulatedCrash):
        await run_at(harness, EVENING)
    store.mark_summary_delivered = original
    await run_at(harness, EVENING + timedelta(minutes=2))
    await run_at(harness, EVENING + timedelta(minutes=4))
    assert len(summaries(harness)) == 1
    owner = (await harness.session()).owner_id
    assert await store.summary_delivered(owner, EVENING.date())
