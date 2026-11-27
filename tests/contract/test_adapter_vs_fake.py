import asyncio
import json

import aiohttp
import pytest

from envoy.domain.telegram import (
    BusinessConnectionUpdate,
    BusinessMessageUpdate,
    CallbackUpdate,
    InlineButton,
    ReplyMarkup,
    TextEntity,
    UnsupportedUpdate,
)
from envoy.telegram.adapter import AiogramAdapter, BotApiError
from envoy.telegram.mapping import ALLOWED_UPDATES
from envoy.telegram.outbound import RetryAfter
from envoy.telegram.ports import TopicUnavailable


@pytest.fixture
async def adapter(fake_bot_api):
    transport = AiogramAdapter("123456:TEST", fake_bot_api.base_url)
    try:
        yield transport
    finally:
        await transport.close()


async def test_adapter_sends_message_via_fake(adapter, fake_bot_api) -> None:
    message = await adapter.send_message(
        42, "hello", reply_markup=ReplyMarkup(((("yes", "yes"),),))
    )
    assert message.chat_id == 42
    assert message.text == "hello"
    assert fake_bot_api.calls[-1].method == "sendMessage"


async def test_adapter_yields_injected_update(adapter, fake_bot_api) -> None:
    fake_bot_api.inject_update(
        {
            "update_id": 7,
            "message": {
                "message_id": 3,
                "date": 1_767_225_600,
                "chat": {"id": 42, "type": "private"},
                "text": "hello",
            },
        }
    )
    updates = adapter.updates()
    update = await asyncio.wait_for(anext(updates), 1)
    await updates.aclose()
    assert update.update_id == 7
    assert update.text == "hello"


async def test_adapter_maps_429_to_retry_after(adapter, fake_bot_api) -> None:
    fake_bot_api.fail_next("sendMessage", 429, retry_after=2)
    with pytest.raises(RetryAfter) as error:
        await adapter.send_message(42, "hello")
    assert error.value.seconds == 2


async def test_adapter_call_raw_unknown_method_hits_fake_with_method_name(
    adapter, fake_bot_api
) -> None:
    with pytest.raises(BotApiError):
        await adapter.call_raw("futureMethod", parameter="hello")
    assert fake_bot_api.calls[-1].method == "futureMethod"
    assert fake_bot_api.calls[-1].params == {"parameter": "hello"}


@pytest.mark.parametrize(
    "body",
    [
        {"update_id": 8, "unknown": {}},
        {
            "update_id": 9,
            "message": {
                "message_id": 3,
                "date": 1,
                "chat": {"id": 42, "type": "private"},
                "paid_messages_waived": {},
            },
        },
    ],
)
async def test_adapter_maps_unknown_update_to_ignored_dto(adapter, fake_bot_api, body) -> None:
    fake_bot_api.inject_update(body)
    updates = adapter.updates()
    update = await anext(updates)
    await updates.aclose()
    assert isinstance(update, UnsupportedUpdate) or update.content_kind == "service"


async def test_secret_token_header_sent_for_webhook_mode(
    adapter, fake_bot_api, aiohttp_server
) -> None:
    await adapter.set_webhook("https://example.org/webhook", "expected-secret")
    assert fake_bot_api.webhook["secret_token"] == "expected-secret"
    app = adapter.webhook_app("expected-secret")
    server = await aiohttp_server(app)
    async with aiohttp.ClientSession() as client:
        async with client.post(server + "/webhook", json={"update_id": 1}) as response:
            assert response.status == 401
        async with client.post(
            server + "/webhook",
            json={"update_id": 1},
            headers={"X-Telegram-Bot-Api-Secret-Token": "expected-secret"},
        ) as response:
            assert response.status == 200


async def test_polling_recovers_after_transient_failure(adapter, fake_bot_api) -> None:
    fake_bot_api.fail_next("getUpdates", 503)
    fake_bot_api.inject_update(
        {"update_id": 77, "message": {"chat": {"id": 42}, "text": "after outage"}}
    )
    updates = adapter.updates()
    update = await asyncio.wait_for(anext(updates), timeout=2)
    await updates.aclose()
    assert update.update_id == 77
    assert len([call for call in fake_bot_api.calls if call.method == "getUpdates"]) == 2


async def next_update(adapter, skip: int = 0):
    updates = adapter.updates()
    try:
        for _ in range(skip):
            await asyncio.wait_for(anext(updates), 2)
        return await asyncio.wait_for(anext(updates), 2)
    finally:
        await updates.aclose()


async def test_adapter_requests_business_allowed_updates(adapter, fake_bot_api) -> None:
    fake_bot_api.inject_update({"update_id": 1, "unknown": {}})
    await next_update(adapter)
    call = next(call for call in fake_bot_api.calls if call.method == "getUpdates")
    assert json.loads(call.params["allowed_updates"]) == list(ALLOWED_UPDATES)
    assert {"business_message", "edited_business_message", "business_connection"} <= set(
        ALLOWED_UPDATES
    )


async def test_adapter_maps_business_message_to_dto(adapter, fake_bot_api) -> None:
    fake_bot_api.inject_business_message("conn", 77, 77, "давай в 10", update_id=5)
    update = await next_update(adapter)
    assert update == BusinessMessageUpdate(
        update_id=5,
        connection_id="conn",
        chat_id=77,
        user_id=77,
        message_id=5,
        text="давай в 10",
    )
    fake_bot_api.inject_business_message("conn", 77, 77, "поправка", update_id=6, edited=True)
    assert (await next_update(adapter, skip=1)).edited is True


async def test_adapter_marks_messages_sent_by_business_bot(adapter, fake_bot_api) -> None:
    fake_bot_api.inject_business_message(
        "conn", 77, 1, "от агента", update_id=5, extra={"sender_business_bot": {"id": 123}}
    )
    assert (await next_update(adapter)).is_bot is True


async def test_adapter_maps_checklist_done_ids(adapter, fake_bot_api) -> None:
    fake_bot_api.inject_business_message(
        "conn",
        77,
        77,
        update_id=8,
        extra={"checklist_tasks_done": {"marked_as_done_task_ids": [2]}},
    )
    update = await next_update(adapter)
    assert update.done_task_ids == (2,)
    assert update.content_kind == "service"


async def test_adapter_maps_paid_messages_waived_to_service_kind(adapter, fake_bot_api) -> None:
    fake_bot_api.inject_business_message(
        "conn", 77, 77, update_id=9, extra={"paid_messages_waived": {}}
    )
    update = await next_update(adapter)
    assert update.content_kind == "service"
    assert update.text is None


async def test_adapter_extracts_card_and_ton_entities(adapter, fake_bot_api) -> None:
    fake_bot_api.inject_business_message(
        "conn",
        77,
        77,
        "card 4111111111111111 ton X",
        update_id=10,
        entities=[
            {"type": "bank_card_number", "offset": 5, "length": 16},
            {"type": "ton_address", "offset": 26, "length": 1},
            {"type": "bold", "offset": 0, "length": 4},
        ],
    )
    update = await next_update(adapter)
    assert update.entities == (
        TextEntity("bank_card_number", 5, 16),
        TextEntity("ton_address", 26, 1),
    )


async def test_adapter_maps_callback_and_connection(adapter, fake_bot_api) -> None:
    fake_bot_api.inject_callback(1, "approve:5", update_id=11, message_id=3)
    update = await next_update(adapter)
    assert update == CallbackUpdate(11, "cb11", 1, 1, 3, "approve:5")
    fake_bot_api.inject_update(
        {
            "update_id": 12,
            "business_connection": {
                "id": "conn",
                "user": {"id": 1, "is_bot": False, "first_name": "Owner"},
                "user_chat_id": 1,
                "date": 1,
                "rights": {"can_reply": True},
                "is_enabled": True,
            },
        }
    )
    connection = await next_update(adapter, skip=1)
    assert isinstance(connection, BusinessConnectionUpdate)
    assert connection.connection.owner_user_id == 1
    assert connection.connection.rights == {"can_reply": True}
    fake_bot_api.inject_update({"update_id": 13, "business_message": {"chat": "broken"}})
    assert await next_update(adapter, skip=2) == UnsupportedUpdate(13)


async def test_adapter_sends_with_business_connection_and_thread(adapter, fake_bot_api) -> None:
    fake_bot_api.inject_business_message("conn", 77, 77, "hi")
    await adapter.send_message(
        77,
        "ответ",
        business_connection_id="conn",
        reply_markup=ReplyMarkup(((InlineButton("Да", "approve:1", "success"),),)),
    )
    params = fake_bot_api.calls[-1].params
    assert params["business_connection_id"] == "conn"
    assert json.loads(params["reply_markup"]) == {
        "inline_keyboard": [[{"text": "Да", "callback_data": "approve:1", "style": "success"}]]
    }
    await adapter.send_message(1, "в топик", thread_id=500)
    assert str(fake_bot_api.calls[-1].params["message_thread_id"]) == "500"


async def test_adapter_business_send_after_window_rejected(adapter, fake_bot_api) -> None:
    with pytest.raises(BotApiError):
        await adapter.send_message(77, "ответ", business_connection_id="conn")


async def test_adapter_creates_topic_and_checklist_and_answers(adapter, fake_bot_api) -> None:
    assert await adapter.create_topic(1, "Встреча") == 500
    fake_bot_api.fail_next("createForumTopic", 400)
    with pytest.raises(TopicUnavailable):
        await adapter.create_topic(1, "Встреча")
    fake_bot_api.inject_business_message("conn", 77, 77, "hi")
    sent = await adapter.send_checklist(77, "conn", "Встреча", ["я приду", "вы придёте"])
    assert sent.chat_id == 77
    tasks = fake_bot_api.checklists[-1]["tasks"]
    assert [task["text"] for task in tasks] == ["я приду", "вы придёте"]
    assert [task["id"] for task in tasks] == [1, 2]
    await adapter.answer_callback("cb1", "Готово")
    assert fake_bot_api.callback_answers[-1]["callback_query_id"] == "cb1"


async def test_adapter_reads_bot_username_from_get_me(adapter, fake_bot_api) -> None:
    assert await adapter.bot_username() == "envoy_bot"
    assert await adapter.bot_username() == "envoy_bot"
    assert [call.method for call in fake_bot_api.calls].count("getMe") == 1
