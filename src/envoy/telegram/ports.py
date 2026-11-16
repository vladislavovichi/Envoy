from collections.abc import AsyncIterator
from typing import Protocol

from envoy.domain.telegram import ReplyMarkup, SentMessage, SurfaceCapabilities, Update


class UpdateSource(Protocol):
    def updates(self) -> AsyncIterator[Update]: ...


class MessageSender(Protocol):
    async def send_message(
        self, chat_id: int, text: str, *, reply_markup: ReplyMarkup | None = None
    ) -> SentMessage: ...


class ChatSurface(Protocol):
    @property
    def capabilities(self) -> SurfaceCapabilities: ...
