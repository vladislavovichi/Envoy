import asyncio
import json
import random
import string
import zlib
from datetime import UTC, datetime, timedelta
from pathlib import Path

import aiohttp
import pytest

from envoy.domain.telegram import InlineButton, ReplyMarkup, WidgetQueryUpdate
from envoy.ports.clock import SystemClock
from envoy.telegram.adapter import AiogramAdapter
from envoy.telegram.dependencies import REGISTRY
from envoy.telegram.mapping import ALLOWED_UPDATES, map_update
from envoy.telegram.outbound import OutboundQueue, QueuedWidgetTransport
from envoy.telegram.ports import QueryExpired, WidgetRejected

SCHEMA = json.loads(Path("tests/fixtures/widget_schema.json").read_text("utf-8"))
METHODS = set(json.loads(Path("tests/fixtures/bot_api_methods.json").read_text("utf-8")))
FAR = datetime.now(UTC) + timedelta(days=1)


@pytest.fixture
async def adapter(fake_bot_api):
    transport = AiogramAdapter("123456:TEST", fake_bot_api.base_url)
    try:
        yield transport
    finally:
        await transport.close()


async def send(adapter, html="<b>x</b>", state="{}", caption="c", **kwargs):
    return await adapter.send_widget(
        7,
        html,
        state,
        caption,
        thread_id=kwargs.pop("thread_id", None),
        allow_queries=True,
        **kwargs,
    )


def entropy_html(compressed: int) -> str:
    rng = random.Random(3)
    text = "".join(rng.choice(string.ascii_letters) for _ in range(8000))
    for length in range(compressed, len(text)):
        if len(zlib.compress(text[:length].encode(), 9)) == compressed:
            return text[:length]
    raise AssertionError("no prefix with the requested compressed size")


def assert_schema(call) -> None:
    spec = SCHEMA["methods"][call.method]
    allowed = set(spec["required"]) | set(spec["optional"])
    assert set(spec["required"]) <= call.params.keys()
    assert call.params.keys() <= allowed


async def test_send_widget_shape_and_returned_message_id(adapter, fake_bot_api) -> None:
    markup = ReplyMarkup(((InlineButton("Одобрить", "approve:42", "success"),),))
    response_id = await send(adapter, thread_id=500, reply_markup=markup, state='{"r":1}')
    call = fake_bot_api.calls[-1]
    assert call.method == "sendWidget"
    assert_schema(call)
    sent_message_id = max(fake_bot_api.widgets)
    assert response_id == sent_message_id
    widget = fake_bot_api.widgets[sent_message_id]
    assert widget["chat_data"] == '{"r":1}'
    assert widget["allow_queries"] is True
    assert int(widget["message_thread_id"]) == 500
    assert widget["reply_markup"]["inline_keyboard"][0][0]["callback_data"] == "approve:42"


async def test_edit_shape_and_repeated_data_is_one_edit(adapter, fake_bot_api) -> None:
    message_id = await send(adapter)
    await adapter.edit_widget(7, message_id, '{"r":2}')
    assert_schema(fake_bot_api.calls[-1])
    await adapter.edit_widget(7, message_id, '{"r":2}')
    assert len(fake_bot_api.widget_edits) == 1
    with pytest.raises(WidgetRejected):
        await adapter.edit_widget(8, message_id, '{"r":3}')


async def test_answer_shape_and_fifteen_second_window(adapter, fake_bot_api) -> None:
    clock = [1_000.0]
    fake_bot_api.now = lambda: clock[0]
    first = fake_bot_api.inject_widget_query(7, 1, "a:42")
    clock[0] += 14.9
    await adapter.answer_widget(first, "ok", deadline=FAR)
    assert_schema(fake_bot_api.calls[-1])
    assert fake_bot_api.widget_answers[-1] == {"widget_query_id": first, "data": "ok"}
    second = fake_bot_api.inject_widget_query(7, 1, "a:43")
    clock[0] += 15
    with pytest.raises(QueryExpired):
        await adapter.answer_widget(second, "ok", deadline=FAR)
    with pytest.raises(QueryExpired):
        await adapter.answer_widget("unknown", "ok", deadline=FAR)


async def test_answer_after_local_deadline_is_not_sent(adapter, fake_bot_api) -> None:
    query = fake_bot_api.inject_widget_query(7, 1, "a:42")
    calls = len(fake_bot_api.calls)
    with pytest.raises(QueryExpired):
        await adapter.answer_widget(query, "ok", deadline=datetime.now(UTC) - timedelta(seconds=1))
    assert len(fake_bot_api.calls) == calls


async def test_answer_size_limit(adapter, fake_bot_api) -> None:
    query = fake_bot_api.inject_widget_query(7, 1, "a:42")
    with pytest.raises(WidgetRejected):
        await adapter.answer_widget(query, "я" * 513, deadline=FAR)
    await adapter.answer_widget(query, "z" * 1024, deadline=FAR)


async def test_widget_query_via_polling_maps_data_and_wallet(adapter, fake_bot_api) -> None:
    fake_bot_api.inject_widget_query(7, 11, "a:42", update_id=70)
    fake_bot_api.inject_widget_query(7, 11, "a:43", query_type="sign_data", update_id=71)
    updates = adapter.updates()
    mapped = await asyncio.wait_for(anext(updates), 2)
    mapped_wallet = await asyncio.wait_for(anext(updates), 2)
    await updates.aclose()
    assert isinstance(mapped, WidgetQueryUpdate)
    assert (mapped.query_id, mapped.user_id, mapped.chat_id, mapped.message_id) == (
        "wq70",
        7,
        7,
        11,
    )
    assert mapped.data == "a:42"
    assert mapped_wallet.data is None
    polling = next(call for call in fake_bot_api.calls if call.method == "getUpdates")
    assert "widget_query" in polling.params["allowed_updates"]


async def test_widget_query_via_webhook(adapter, fake_bot_api, aiohttp_server) -> None:
    await adapter.set_webhook("https://example.org/webhook", "secret")
    assert "widget_query" in fake_bot_api.webhook["allowed_updates"]
    server = await aiohttp_server(adapter.webhook_app("secret"))
    raw = {
        "update_id": 5,
        "widget_query": {
            "id": "q5",
            "from": {"id": 7, "is_bot": False, "first_name": "U"},
            "chat": {"id": 7, "type": "private"},
            "message_id": 3,
            "chat_instance": "1",
            "type": {"type": "data"},
            "data": "d:9",
        },
    }
    async with aiohttp.ClientSession() as client:
        async with client.post(
            server + "/webhook", json=raw, headers={"X-Telegram-Bot-Api-Secret-Token": "secret"}
        ) as response:
            assert response.status == 200
    updates = adapter.updates()
    mapped = await asyncio.wait_for(anext(updates), 1)
    await updates.aclose()
    assert isinstance(mapped, WidgetQueryUpdate)
    assert mapped.data == "d:9"


def test_mapping_tolerates_missing_message_and_malformed_query() -> None:
    received = datetime(2026, 1, 1, tzinfo=UTC)
    mapped = map_update(
        {
            "update_id": 1,
            "widget_query": {
                "id": "q",
                "from": {"id": 1},
                "chat_instance": "x",
                "type": {"type": "data"},
                "data": "a:1",
            },
        },
        received,
    )
    assert isinstance(mapped, WidgetQueryUpdate)
    assert (mapped.chat_id, mapped.message_id, mapped.received_at) == (None, None, received)
    assert map_update({"update_id": 2, "widget_query": {"id": "q"}}).kind == "unsupported"
    assert "widget_query" in ALLOWED_UPDATES


@pytest.mark.parametrize(("size", "accepted"), [(1024, True), (1025, False)])
async def test_state_limit_in_utf8_bytes(adapter, size, accepted) -> None:
    state = "я" * (size // 2) + "x" * (size % 2)
    assert len(state.encode()) == size
    if accepted:
        await send(adapter, state=state)
    else:
        with pytest.raises(WidgetRejected):
            await send(adapter, state=state)


@pytest.mark.parametrize(("size", "accepted"), [(32768, True), (32769, False)])
async def test_raw_html_limit(adapter, size, accepted) -> None:
    html = "a" * size
    if accepted:
        await send(adapter, html=html)
    else:
        with pytest.raises(WidgetRejected):
            await send(adapter, html=html)


@pytest.mark.parametrize(("compressed", "accepted"), [(4096, True), (4097, False)])
async def test_compressed_html_limit(adapter, compressed, accepted) -> None:
    html = entropy_html(compressed)
    if accepted:
        await send(adapter, html=html)
    else:
        with pytest.raises(WidgetRejected):
            await send(adapter, html=html)


async def test_nul_is_rejected_in_html_and_state(adapter, fake_bot_api) -> None:
    with pytest.raises(WidgetRejected):
        await send(adapter, html="<b>\x00</b>")
    with pytest.raises(WidgetRejected):
        await send(adapter, state='{"a":"\x00"}')
    message_id = await send(adapter)
    with pytest.raises(WidgetRejected):
        await adapter.edit_widget(7, message_id, "\x00")


async def test_caption_limit(adapter) -> None:
    await send(adapter, caption="я" * 1024)
    with pytest.raises(WidgetRejected):
        await send(adapter, caption="я" * 1025)


def test_request_size_and_rate_window(fake_bot_api) -> None:
    clock = [100.0]
    fake_bot_api.now = lambda: clock[0]
    assert fake_bot_api.inject_widget_query(1, 1, "x" * 64) is not None
    with pytest.raises(ValueError, match="1-64"):
        fake_bot_api.inject_widget_query(1, 1, "x" * 65)
    with pytest.raises(ValueError, match="1-64"):
        fake_bot_api.inject_widget_query(1, 1, "")
    for _ in range(4):
        assert fake_bot_api.inject_widget_query(1, 1, "a:1") is not None
    assert fake_bot_api.inject_widget_query(1, 1, "a:1") is None
    assert fake_bot_api.inject_widget_query(2, 1, "a:1") is not None
    clock[0] += 5
    assert fake_bot_api.inject_widget_query(1, 1, "a:1") is not None


def test_widget_methods_registered_with_version_and_present_in_fixture() -> None:
    widget = {item.method: item.since_api for item in REGISTRY if "widget" in item.used_in}
    assert widget == {
        "sendWidget": "10.4",
        "editMessageWidgetData": "10.4",
        "answerWidgetQuery": "10.4",
    }
    assert set(widget) <= METHODS
    assert set(widget) == set(SCHEMA["methods"])
    assert SCHEMA["api_version"] == "10.4"


async def test_queued_widget_transport_uses_chat_queue_and_answers_directly(adapter) -> None:
    queue = OutboundQueue(adapter, 1000, 1000, 1, SystemClock())
    calls: list[str] = []

    class Recording:
        async def send_widget(self, *args, **kwargs):
            calls.append("send")
            return 5

        async def edit_widget(self, *args):
            calls.append("edit")

        async def answer_widget(self, query_id, data, *, deadline):
            calls.append("answer")

    transport = QueuedWidgetTransport(Recording(), queue)
    try:
        assert (
            await transport.send_widget(1, "h", "{}", "c", thread_id=None, allow_queries=False) == 5
        )
        await transport.edit_widget(1, 5, "{}")
        await transport.answer_widget("q", "ok", deadline=FAR)
    finally:
        await queue.close()
    assert calls == ["send", "edit", "answer"]


async def test_bot_to_bot_message_maps_with_own_bot_id_and_description(fake_bot_api) -> None:
    from envoy.domain.telegram import BotMessageUpdate

    adapter = AiogramAdapter("123456:TEST", fake_bot_api.base_url)
    try:
        fake_bot_api.inject_bot_message(777, "envoy/1:abc", update_id=90)
        fake_bot_api.inject_update(
            {
                "update_id": 91,
                "message": {
                    "message_id": 1,
                    "date": 1,
                    "chat": {"id": -5, "type": "group"},
                    "from": {"id": 778, "is_bot": True, "first_name": "Bot"},
                    "text": "envoy/1:abc",
                },
            }
        )
        updates = adapter.updates()
        first = await asyncio.wait_for(anext(updates), 2)
        second = await asyncio.wait_for(anext(updates), 2)
        await updates.aclose()
        assert first == BotMessageUpdate(
            bot_id=123456,
            update_id=90,
            from_bot_id=777,
            chat_id=777,
            text="envoy/1:abc",
            received_at=first.received_at,
        )
        assert not isinstance(second, BotMessageUpdate)
        await adapter.set_description("Envoy — ИИ-агент. Ключ агента: abcd")
        assert fake_bot_api.description.endswith("abcd")
    finally:
        await adapter.close()
