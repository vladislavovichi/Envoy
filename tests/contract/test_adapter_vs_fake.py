import asyncio

import aiohttp
import pytest

from envoy.domain.telegram import ReplyMarkup
from envoy.telegram.adapter import AiogramAdapter, BotApiError
from envoy.telegram.outbound import RetryAfter


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
    assert (await anext(updates)).ignored is True
    await updates.aclose()


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
