from collections.abc import AsyncIterator, Sequence
from datetime import datetime
from typing import Protocol

from envoy.domain.telegram import ReplyMarkup, SentMessage, SurfaceCapabilities, Update


class TopicUnavailable(Exception):
    pass


class SendRejected(Exception):
    pass


class WidgetRejected(SendRejected):
    pass


class QueryExpired(Exception):
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


class WidgetTransport(Protocol):
    async def send_widget(
        self,
        chat_id: int,
        html: str,
        state: str,
        caption: str,
        *,
        thread_id: int | None,
        allow_queries: bool,
        reply_markup: ReplyMarkup | None = None,
    ) -> int: ...

    async def edit_widget(self, chat_id: int, message_id: int, state: str) -> None: ...

    async def answer_widget(self, query_id: str, data: str, *, deadline: datetime) -> None: ...
