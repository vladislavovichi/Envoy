import json

import pytest
import structlog
from pydantic import SecretStr

from envoy.observability.logging import configure_logging


@pytest.mark.parametrize(
    "payload",
    [
        {"weights": {"price": 1}},
        {"nested": {"reservation": 0.5}},
        {"token": "secret"},
        {"ENVOY_MASTER_KEY": "secret"},
        {"nested": [SecretStr("secret")]},
        {"value": SecretStr("secret")},
    ],
)
def test_private_fields_and_secret_str_never_rendered(payload, capsys) -> None:
    configure_logging(frozenset())
    structlog.get_logger().info("event", **payload)
    output = capsys.readouterr().out
    assert "secret" not in output
    assert "***" in output
    assert "price" not in output


def test_non_private_field_kept(capsys) -> None:
    configure_logging(frozenset())
    structlog.get_logger().info("event", session="visible")
    assert json.loads(capsys.readouterr().out)["session"] == "visible"


@pytest.mark.parametrize("card", ["4111111111111111", "4111 1111 1111 1111", "4111-1111-1111-1111"])
def test_card_number_in_text_masked_in_log(card: str, capsys) -> None:
    configure_logging(frozenset())
    structlog.get_logger().info("pay " + card, nested={"items": ["card " + card]})
    output = capsys.readouterr().out
    assert card not in output
    assert output.count("[card]") == 2


def test_digits_failing_luhn_kept(capsys) -> None:
    configure_logging(frozenset())
    structlog.get_logger().info("4111111111111112")
    assert "4111111111111112" in capsys.readouterr().out


def test_database_password_environment_field_is_masked(capsys) -> None:
    configure_logging()
    structlog.get_logger().info("event", ENVOY_POSTGRES_PASSWORD="private-database-password")
    assert "private-database-password" not in capsys.readouterr().out


def test_nested_test_database_url_environment_field_is_masked(capsys) -> None:
    configure_logging()
    structlog.get_logger().info(
        "event", config={"ENVOY_TEST_DATABASE_URL": "postgresql://user:CANARY@host/db"}
    )
    assert "CANARY" not in capsys.readouterr().out


def test_database_user_environment_field_is_masked(capsys) -> None:
    configure_logging()
    structlog.get_logger().info("event", ENVOY_POSTGRES_USER="CREDENTIAL_CANARY")
    assert "CREDENTIAL_CANARY" not in capsys.readouterr().out


@pytest.mark.parametrize("text", ["4111 1111 1111 1111 2", "1 4111 1111 1111 1111"])
def test_card_next_to_numeric_reference_is_still_masked(text: str, capsys) -> None:
    configure_logging()
    structlog.get_logger().info(text)
    output = capsys.readouterr().out
    assert "4111 1111 1111 1111" not in output
    assert "[card]" in output
