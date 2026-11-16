import aiohttp


async def test_get_me_returns_bot(fake_bot_api) -> None:
    async with aiohttp.ClientSession() as client:
        async with client.post(f"{fake_bot_api.base_url}/bot123:test/getMe") as response:
            body = await response.json()
    assert body["ok"] is True
    assert body["result"]["is_bot"] is True


async def test_send_message_records_call_and_returns_message(fake_bot_api) -> None:
    async with aiohttp.ClientSession() as client:
        async with client.post(
            f"{fake_bot_api.base_url}/bot123:test/sendMessage",
            json={"chat_id": 42, "text": "hello"},
        ) as response:
            body = await response.json()
    assert body["result"]["text"] == "hello"
    assert body["result"]["chat"]["id"] == 42
    assert fake_bot_api.calls[-1].method == "sendMessage"
    assert fake_bot_api.calls[-1].params["text"] == "hello"


async def test_get_updates_returns_injected_in_order_and_acks_offset(fake_bot_api) -> None:
    fake_bot_api.inject_update({"update_id": 2})
    fake_bot_api.inject_update({"update_id": 1})
    async with aiohttp.ClientSession() as client:
        url = f"{fake_bot_api.base_url}/bot123:test/getUpdates"
        async with client.get(url) as response:
            assert [item["update_id"] for item in (await response.json())["result"]] == [1, 2]
        async with client.post(url, data={"offset": 2}) as response:
            assert (await response.json())["result"] == [{"update_id": 2}]
        async with client.post(url, json={"offset": 3}) as response:
            assert (await response.json())["result"] == []


async def test_fail_next_returns_429_with_retry_after(fake_bot_api) -> None:
    fake_bot_api.fail_next("sendMessage", 429, retry_after=3)
    async with aiohttp.ClientSession() as client:
        async with client.post(f"{fake_bot_api.base_url}/bot123:test/sendMessage") as response:
            assert response.status == 429
            body = await response.json()
    assert body["ok"] is False
    assert body["parameters"]["retry_after"] == 3


async def test_unknown_method_returns_404_envelope(fake_bot_api) -> None:
    async with aiohttp.ClientSession() as client:
        async with client.post(f"{fake_bot_api.base_url}/bot123:test/missing") as response:
            assert response.status == 404
            assert (await response.json())["error_code"] == 404
