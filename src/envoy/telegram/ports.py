from collections.abc import AsyncIterator, Sequence
from typing import Protocol

from envoy.domain.telegram import ReplyMarkup, SentMessage, SurfaceCapabilities, Update


class TopicUnavailable(Exception):
    pass


class SendRejected(Exception):
    pass


class UpdateSource(Protocol):
    def updates(self) -> AsyncIterator[Update]: ...


class MessageSender(Protocol):
    async def send_message(
        self,
        chat_id: int,
        text: str,
        *,
        reply_markup: ReplyMarkup | None = None,
        business_connection_id: str | None = None,
        thread_id: int | None = None,
    ) -> SentMessage: ...


class TopicCreator(Protocol):
    async def create_topic(self, chat_id: int, title: str) -> int: ...


class ChecklistSender(Protocol):
    async def send_checklist(
        self, chat_id: int, business_connection_id: str, title: str, items: Sequence[str]
    ) -> SentMessage: ...


class CallbackAnswerer(Protocol):
    async def answer_callback(self, callback_id: str, text: str | None = None) -> None: ...


class BotIdentity(Protocol):
    async def bot_username(self) -> str: ...


class ChatSurface(Protocol):
    @property
    def capabilities(self) -> SurfaceCapabilities: ...
