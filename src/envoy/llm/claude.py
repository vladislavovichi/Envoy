from typing import Any, Literal

import anthropic
from anthropic import AsyncAnthropic
from anthropic.types.beta import BetaOutputConfigParam, BetaUserMessageParam
from pydantic import BaseModel, ValidationError

from envoy.llm.ports import LanguageModelError, StructuredRequest

FALLBACK_BETA = "server-side-fallback-2026-07-01"
DROPPED_KEYWORDS = frozenset(
    {"title", "default", "minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum"}
)


def strict_schema(schema: dict[str, Any]) -> dict[str, Any]:
    def visit(node: Any) -> Any:
        if isinstance(node, dict):
            result = {
                key: (
                    {name: visit(item) for name, item in value.items()}
                    if key in ("properties", "$defs")
                    else visit(value)
                )
                for key, value in node.items()
                if key not in DROPPED_KEYWORDS
            }
            if result.get("type") == "object" and "properties" in result:
                result["additionalProperties"] = False
                result["required"] = sorted(result["properties"])
            return result
        if isinstance(node, list):
            return [visit(item) for item in node]
        return node

    converted: dict[str, Any] = visit(schema)
    return converted


class ClaudeLanguageModel:
    def __init__(
        self,
        client: AsyncAnthropic,
        model: str,
        timeout: float,
        effort: Literal["low", "medium", "high", "xhigh", "max"] = "low",
    ) -> None:
        self._client = client
        self._model = model
        self._timeout = timeout
        self._effort = effort

    async def complete[T: BaseModel](self, request: StructuredRequest[T]) -> T:
        message: BetaUserMessageParam = {"role": "user", "content": request.data}
        output: BetaOutputConfigParam = {
            "effort": self._effort,
            "format": {
                "type": "json_schema",
                "schema": strict_schema(request.schema.model_json_schema()),
            },
        }
        try:
            response = await self._client.with_options(timeout=self._timeout).beta.messages.create(
                model=self._model,
                max_tokens=request.max_tokens,
                system=request.system,
                messages=[message],
                output_config=output,
                betas=[FALLBACK_BETA],
                fallbacks="default",
            )
        except anthropic.AnthropicError:
            raise LanguageModelError("Language model request failed") from None
        if response.stop_reason != "end_turn":
            raise LanguageModelError(f"Language model stopped with {response.stop_reason}")
        text = next((block.text for block in response.content if block.type == "text"), None)
        if text is None:
            raise LanguageModelError("Language model returned no text")
        try:
            return request.schema.model_validate_json(text)
        except ValidationError:
            raise LanguageModelError("Language model output failed validation") from None
