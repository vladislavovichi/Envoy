from collections.abc import Mapping, MutableMapping
from typing import Any

import structlog
from pydantic import SecretStr

from envoy.security.payment import CARD_LABEL, TON_LABEL, payment_spans

PRIVATE_FIELDS = frozenset(
    {
        "reservation",
        "weights",
        "opponent_model",
        "mandate",
        "utility",
        "private_key",
        "token",
        "bot_token",
        "agent_key",
        "master_key",
        "database_url",
        "test_database_url",
        "postgres_password",
        "postgres_user",
        "password",
        "secret_token",
        "anthropic_api_key",
        "api_key",
    }
)
LOG_LABELS = {CARD_LABEL: "[card]", TON_LABEL: "[ton]"}


def mask_cards(value: str) -> str:
    pieces: list[str] = []
    offset = 0
    for start, end, label in payment_spans(value):
        pieces.extend([value[offset:start], LOG_LABELS[label]])
        offset = end
    return "".join(pieces) + value[offset:]


def redact(value: Any, private_fields: frozenset[str]) -> Any:
    if isinstance(value, SecretStr):
        return "***"
    if isinstance(value, Mapping):
        return {
            key: (
                "***"
                if str(key).lower().removeprefix("envoy_") in private_fields
                else redact(item, private_fields)
            )
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [redact(item, private_fields) for item in value]
    if isinstance(value, str):
        return mask_cards(value)
    return value


def configure_logging(private_fields: frozenset[str] = frozenset()) -> None:
    fields = PRIVATE_FIELDS | private_fields

    def processor(logger: Any, method: str, event: MutableMapping[str, Any]) -> dict[str, Any]:
        result: dict[str, Any] = redact(event, fields)
        return result

    structlog.configure(
        processors=[processor, structlog.processors.JSONRenderer()],
        logger_factory=structlog.PrintLoggerFactory(),
        cache_logger_on_first_use=False,
    )
