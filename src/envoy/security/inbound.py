from dataclasses import dataclass, replace
from typing import Literal, Protocol

from aiolimiter import AsyncLimiter

from envoy.domain.telegram import (
    BusinessConnectionUpdate,
    BusinessMessageUpdate,
    CallbackUpdate,
    MessageUpdate,
    UnsupportedUpdate,
    Update,
)
from envoy.security.payment import redact_payment_details

type SenderClass = Literal["owner", "counterpart", "owner_in_business_chat"]
type DropReason = Literal[
    "not_text", "too_long", "bot_sender", "rate_limited", "unknown_sender", "edited"
]


class OwnerDirectory(Protocol):
    async def is_owner(self, user_id: int) -> bool: ...
    async def connection_owner(self, connection_id: str) -> int | None: ...


@dataclass(frozen=True)
class Passed:
    update: Update
    sender: SenderClass
    payment_details_redacted: bool = False


@dataclass(frozen=True)
class Dropped:
    reason: DropReason


class InboundFilter:
    def __init__(self, owners: OwnerDirectory, max_text_length: int, rate_per_minute: int) -> None:
        if max_text_length <= 0 or rate_per_minute <= 0:
            raise ValueError("Limits must be positive")
        self._owners = owners
        self._max_text_length = max_text_length
        self._rate = rate_per_minute
        self._limiters: dict[int, AsyncLimiter] = {}

    async def _admit(self, user_id: int) -> bool:
        limiter = self._limiters.get(user_id)
        if limiter is None:
            limiter = self._limiters[user_id] = AsyncLimiter(self._rate, 60)
        if not limiter.has_capacity():
            return False
        await limiter.acquire()
        return True

    async def check(self, update: Update) -> Passed | Dropped:
        match update:
            case UnsupportedUpdate():
                return Dropped("not_text")
            case BusinessConnectionUpdate():
                if not await self._owners.is_owner(update.connection.owner_user_id):
                    return Dropped("unknown_sender")
                return Passed(update, "owner")
            case CallbackUpdate():
                if not await self._owners.is_owner(update.user_id):
                    return Dropped("unknown_sender")
                if not await self._admit(update.user_id):
                    return Dropped("rate_limited")
                return Passed(update, "owner")
            case MessageUpdate():
                if not await self._owners.is_owner(update.user_id):
                    return Dropped("unknown_sender")
                return await self._text(update, "owner")
            case BusinessMessageUpdate():
                owner = await self._owners.connection_owner(update.connection_id)
                if owner is None or not await self._owners.is_owner(owner):
                    return Dropped("unknown_sender")
                sender: SenderClass = (
                    "owner_in_business_chat" if update.user_id == owner else "counterpart"
                )
                if update.done_task_ids and not update.edited:
                    return Passed(update, sender)
                return await self._text(update, sender)

    async def _text(
        self, update: MessageUpdate | BusinessMessageUpdate, sender: SenderClass
    ) -> Passed | Dropped:
        if update.edited:
            return Dropped("edited")
        if update.is_bot:
            return Dropped("bot_sender")
        if update.content_kind != "text" or update.text is None:
            return Dropped("not_text")
        if len(update.text) > self._max_text_length:
            return Dropped("too_long")
        if not await self._admit(update.user_id):
            return Dropped("rate_limited")
        text, redacted = redact_payment_details(update.text, update.entities)
        return Passed(replace(update, text=text, entities=()), sender, redacted)
