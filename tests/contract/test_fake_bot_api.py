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


async def call(fake_bot_api, method: str, **params):
    url = f"{fake_bot_api.base_url}/bot123:test/{method}"
    async with aiohttp.ClientSession() as client:
        async with client.post(url, json=params) as response:
            return response.status, await response.json()


async def test_fake_creates_forum_topic(fake_bot_api) -> None:
    status, body = await call(fake_bot_api, "createForumTopic", chat_id=1, name="Сессия")
    assert status == 200
    assert body["result"]["message_thread_id"] == 500
    assert fake_bot_api.topics[0]["name"] == "Сессия"
    status, _ = await call(fake_bot_api, "createForumTopic", chat_id=1)
    assert status == 400


async def test_fake_records_checklist_items(fake_bot_api) -> None:
    fake_bot_api.inject_business_message("conn", 7, 7, "hi")
    checklist = {"title": "Встреча", "tasks": [{"id": 1, "text": "прийти"}]}
    status, body = await call(
        fake_bot_api, "sendChecklist", business_connection_id="conn", chat_id=7, checklist=checklist
    )
    assert status == 200
    assert body["result"]["checklist"] == checklist
    assert fake_bot_api.checklists[0]["tasks"][0]["text"] == "прийти"
    status, _ = await call(fake_bot_api, "sendChecklist", chat_id=7, checklist=checklist)
    assert status == 400


async def test_fake_rejects_business_send_after_window(fake_bot_api) -> None:
    clock = [1_000_000.0]
    fake_bot_api.now = lambda: clock[0]
    params = {"chat_id": 7, "text": "x", "business_connection_id": "c"}
    assert (await call(fake_bot_api, "sendMessage", **params))[0] == 400
    fake_bot_api.inject_business_message("c", 7, 7, "hi")
    status, body = await call(fake_bot_api, "sendMessage", **params)
    assert status == 200
    assert body["result"]["business_connection_id"] == "c"
    clock[0] += 24 * 3600 + 1
    assert (await call(fake_bot_api, "sendMessage", **params))[0] == 400
    fake_bot_api.inject_business_message("c", 7, 1, "outgoing from the owner")
    assert (await call(fake_bot_api, "sendMessage", **params))[0] == 400


async def test_fake_answers_callback(fake_bot_api) -> None:
    update_id = fake_bot_api.inject_callback(1, "approve:3")
    status, body = await call(
        fake_bot_api, "answerCallbackQuery", callback_query_id=f"cb{update_id}"
    )
    assert (status, body["result"]) == (200, True)
    assert fake_bot_api.callback_answers[0]["callback_query_id"] == f"cb{update_id}"
    assert (await call(fake_bot_api, "answerCallbackQuery"))[0] == 400
