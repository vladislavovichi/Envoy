import json
from pathlib import Path

import pytest
from aiohttp import web
from anthropic import AsyncAnthropic
from pydantic import BaseModel

from envoy.llm.claude import ClaudeLanguageModel, strict_schema
from envoy.llm.fake import ScriptedLanguageModel
from envoy.llm.ports import LanguageModel, LanguageModelError, StructuredRequest
from envoy.llm.translator import IntentWire

CASSETTES = json.loads(Path("tests/cassettes/claude_messages.json").read_text(encoding="utf-8"))
REQUEST = StructuredRequest("system", '{"message": "давай в кафе в 10"}', IntentWire, 256)


class LanguageModelContract:
    async def model_for(self, scenario: str, aiohttp_server) -> LanguageModel:
        raise NotImplementedError

    async def test_returns_schema_valid_object(self, aiohttp_server) -> None:
        model = await self.model_for("intent_propose", aiohttp_server)
        result = await model.complete(REQUEST)
        assert isinstance(result, IntentWire)
        assert result.kind == "propose"
        assert {item.issue for item in result.values} == {"time", "place"}

    async def test_invalid_output_raises_language_model_error(self, aiohttp_server) -> None:
        model = await self.model_for("invalid_output", aiohttp_server)
        with pytest.raises(LanguageModelError):
            await model.complete(REQUEST)

    async def test_api_error_raises_language_model_error(self, aiohttp_server) -> None:
        model = await self.model_for("api_error", aiohttp_server)
        with pytest.raises(LanguageModelError):
            await model.complete(REQUEST)


class Maybe(BaseModel):
    kind: str = "maybe"


class TestScriptedLanguageModel(LanguageModelContract):
    async def model_for(self, scenario: str, aiohttp_server) -> LanguageModel:
        match scenario:
            case "intent_propose":
                reply = IntentWire.model_validate_json(
                    CASSETTES[scenario]["body"]["content"][1]["text"]
                )
                return ScriptedLanguageModel([(lambda data: True, reply)])
            case "invalid_output":
                return ScriptedLanguageModel([(lambda data: True, Maybe())])
            case _:
                return ScriptedLanguageModel([(lambda data: False, IntentWire(kind="accept"))])


class TestClaudeLanguageModel(LanguageModelContract):
    requests: list[dict]

    async def model_for(self, scenario: str, aiohttp_server) -> LanguageModel:
        self.requests = []
        cassette = CASSETTES[scenario]

        async def messages(request: web.Request) -> web.Response:
            self.requests.append({"headers": dict(request.headers), "body": await request.json()})
            return web.json_response(cassette["body"], status=cassette["status"])

        app = web.Application()
        app.router.add_post("/v1/messages", messages)
        base = await aiohttp_server(app)
        client = AsyncAnthropic(api_key="test-key", base_url=base, max_retries=0)
        return ClaudeLanguageModel(client, "claude-opus-5-5", timeout=5)

    async def test_request_uses_structured_output_and_data_channel(self, aiohttp_server) -> None:
        model = await self.model_for("intent_propose", aiohttp_server)
        await model.complete(REQUEST)
        body = self.requests[0]["body"]
        assert body["model"] == "claude-opus-5-5"
        assert body["system"] == "system"
        assert body["messages"] == [{"role": "user", "content": REQUEST.data}]
        assert body["output_config"]["format"]["type"] == "json_schema"
        assert body["output_config"]["effort"] == "low"
        assert body["fallbacks"] == "default"
        assert "tool_choice" not in body
        assert "server-side-fallback-2026-07-01" in self.requests[0]["headers"]["anthropic-beta"]

    async def test_refusal_raises_language_model_error(self, aiohttp_server) -> None:
        model = await self.model_for("refusal", aiohttp_server)
        with pytest.raises(LanguageModelError, match="refusal"):
            await model.complete(REQUEST)


def test_strict_schema_closes_objects_and_keeps_property_names() -> None:
    schema = strict_schema(
        {
            "type": "object",
            "title": "X",
            "properties": {"default": {"type": "integer", "minimum": 1, "title": "D"}},
            "$defs": {"title": {"type": "object", "properties": {"a": {"type": "string"}}}},
        }
    )
    assert schema == {
        "type": "object",
        "properties": {"default": {"type": "integer"}},
        "$defs": {
            "title": {
                "type": "object",
                "properties": {"a": {"type": "string"}},
                "additionalProperties": False,
                "required": ["a"],
            }
        },
        "additionalProperties": False,
        "required": ["default"],
    }
