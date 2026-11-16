from functools import partial
from unittest.mock import create_autospec

import pytest
from aiohttp import ClientResponse
from aiohttp.abc import AbstractStreamWriter
from aioresponses import aioresponses

from envoy.telegram.adapter import AiogramAdapter
from envoy.telegram.outbound import RetryAfter


def response_factory():
    writer = create_autospec(AbstractStreamWriter, instance=True)
    writer.output_size = 0
    return partial(ClientResponse, stream_writer=writer)


async def test_adapter_builds_send_message_request_exactly() -> None:
    adapter = AiogramAdapter("123456:TEST", "http://bot-api")
    try:
        with aioresponses() as mocked:
            mocked.post(
                "http://bot-api/bot123456:TEST/sendMessage",
                response_class=response_factory(),
                payload={
                    "ok": True,
                    "result": {
                        "message_id": 1,
                        "date": 1,
                        "chat": {"id": 42, "type": "private"},
                        "text": "hi",
                    },
                },
            )
            assert (await adapter.send_message(42, "hi")).text == "hi"
            request = next(iter(mocked.requests.values()))[0]
            fields = {field[0]["name"]: field[2] for field in request.kwargs["data"]._fields}
            assert fields["chat_id"] == "42"
            assert fields["text"] == "hi"
    finally:
        await adapter.close()


async def test_adapter_parses_error_envelope() -> None:
    adapter = AiogramAdapter("123456:TEST", "http://bot-api")
    try:
        with aioresponses() as mocked:
            mocked.post(
                "http://bot-api/bot123456:TEST/sendMessage",
                response_class=response_factory(),
                status=429,
                payload={
                    "ok": False,
                    "error_code": 429,
                    "description": "retry",
                    "parameters": {"retry_after": 3},
                },
            )
            with pytest.raises(RetryAfter) as error:
                await adapter.send_message(1, "hi")
            assert error.value.seconds == 3
    finally:
        await adapter.close()
