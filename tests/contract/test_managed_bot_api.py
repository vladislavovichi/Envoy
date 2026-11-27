import asyncio
import json

import aiohttp
import pytest

from envoy.domain.telegram import ManagedBotUpdate
from envoy.telegram.adapter import AiogramAdapter, BotApiError
from envoy.telegram.mapping import ALLOWED_UPDATES, map_update


@pytest.fixture
async def manager(fake_bot_api):
    adapter = AiogramAdapter("123456:MANAGER", fake_bot_api.base_url)
    try:
        yield adapter
    finally:
        await adapter.close()


async def test_request_button_token_access_and_profile(manager, fake_bot_api) -> None:
    await manager.request_managed_bot(1, 77, "Мой агент", "envoy1_bot")
    markup = fake_bot_api.calls[-1].params["reply_markup"]
    keyboard = markup if isinstance(markup, dict) else json.loads(markup)
    button = keyboard["keyboard"][0][0]
    assert button["request_managed_bot"] == {
        "request_id": 77,
        "suggested_name": "Мой агент",
        "suggested_username": "envoy1_bot",
    }
    fake_bot_api.inject_managed_bot(1, 9001, "envoy1_bot")
    updates = manager.updates()
    event = await asyncio.wait_for(anext(updates), 2)
    await updates.aclose()
    assert event == ManagedBotUpdate(event.update_id, 1, 9001, "envoy1_bot")
    token = await manager.managed_bot_token(9001)
    managed = AiogramAdapter(token, fake_bot_api.base_url)
    try:
        await managed.set_name("Агент Envoy")
        await managed.set_description("ИИ-агент")
        assert fake_bot_api.profiles[token] == {"name": "Агент Envoy", "description": "ИИ-агент"}
        await manager.set_managed_bot_access(9001, True, list(range(10)))
        assert fake_bot_api.access[9001]["users"] == list(range(10))
        with pytest.raises(BotApiError):
            await manager.set_managed_bot_access(9001, True, list(range(11)))
        replaced = await manager.replace_managed_bot_token(9001)
        assert replaced != token
        with pytest.raises(BotApiError):
            await managed.set_name("старый токен")
    finally:
        await managed.close()
    assert "managed_bot" in ALLOWED_UPDATES


def test_service_message_maps_to_managed_bot_event() -> None:
    raw = {
        "update_id": 5,
        "message": {
            "message_id": 1,
            "date": 1,
            "chat": {"id": 1, "type": "private"},
            "from": {"id": 1, "is_bot": False, "first_name": "Owner"},
            "managed_bot_created": {"bot": {"id": 9002, "is_bot": True, "username": "envoy2_bot"}},
        },
    }
    assert map_update(raw) == ManagedBotUpdate(5, 1, 9002, "envoy2_bot")


async def test_managed_webhook_rejects_foreign_secret(fake_bot_api, aiohttp_server) -> None:
    managed = AiogramAdapter("9001:managed", fake_bot_api.base_url)
    try:
        server = await aiohttp_server(managed.webhook_app("own-secret"))
        async with aiohttp.ClientSession() as client:
            async with client.post(
                server + "/webhook",
                json={"update_id": 1},
                headers={"X-Telegram-Bot-Api-Secret-Token": "other-secret"},
            ) as response:
                assert response.status == 401
    finally:
        await managed.close()
