from math import isfinite
from typing import Annotated, Any, Literal, Self
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_prefix="ENVOY_",
        env_ignore_empty=True,
        extra="ignore",
        hide_input_in_errors=True,
    )
    journal_store: str = "memory"
    telegram_transport: str = "fake"
    database_url: SecretStr | None = None
    bot_token: SecretStr | None = None
    agent_key: SecretStr
    master_key: SecretStr
    global_rate: float = Field(default=30, gt=0)
    per_chat_rate: float = Field(default=1, gt=0)
    max_retry_after: float = Field(default=60, gt=0)
    telegram_api_base: str = "https://api.telegram.org"
    postgres_user: SecretStr | None = None
    postgres_password: SecretStr | None = None
    postgres_db: str | None = None
    postgres_port: int = Field(default=5433, ge=1, le=65535)
    test_database_url: SecretStr | None = None
    owner_ids: Annotated[frozenset[int], NoDecode] = frozenset()
    default_timezone: str = "Europe/Moscow"
    agent_name: str = "Envoy"
    llm: str = "fake"
    llm_model: str = "claude-opus-5-5"
    anthropic_api_key: SecretStr | None = None
    llm_daily_token_budget: int = Field(default=200_000, gt=0)
    inbound_max_text_length: int = Field(default=2000, gt=0)
    inbound_rate_per_minute: int = Field(default=20, gt=0)
    owner_view: Literal["text", "widget"] = "text"
    widget_topic_links: Literal["direct", "request"] = "request"
    summary_hour: int = Field(default=20, ge=0, le=23)

    @field_validator("owner_ids", mode="before")
    @classmethod
    def split_owner_ids(cls, value: Any) -> Any:
        if isinstance(value, str):
            return frozenset(int(item) for item in value.replace(" ", "").split(",") if item)
        return value

    @field_validator("default_timezone")
    @classmethod
    def known_timezone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except ZoneInfoNotFoundError, ValueError:
            raise ValueError("Unknown timezone") from None
        return value

    @field_validator("agent_key", "master_key")
    @classmethod
    def valid_key(cls, value: SecretStr) -> SecretStr:
        try:
            raw = bytes.fromhex(value.get_secret_value())
        except ValueError:
            raise ValueError("Key must contain 32 bytes encoded as hexadecimal") from None
        if len(raw) != 32:
            raise ValueError("Key must contain 32 bytes encoded as hexadecimal")
        return value

    @field_validator("global_rate", "per_chat_rate", "max_retry_after")
    @classmethod
    def finite_rate(cls, value: float) -> float:
        if not isfinite(value):
            raise ValueError("Rate must be finite")
        return value

    @model_validator(mode="after")
    def required_secrets(self) -> Self:
        if self.journal_store == "postgres" and self.database_url is None:
            raise ValueError("ENVOY_DATABASE_URL is required for postgres")
        if self.telegram_transport == "aiogram" and self.bot_token is None:
            raise ValueError("ENVOY_BOT_TOKEN is required for aiogram")
        if self.llm == "anthropic" and self.anthropic_api_key is None:
            raise ValueError("ENVOY_ANTHROPIC_API_KEY is required for anthropic")
        return self
