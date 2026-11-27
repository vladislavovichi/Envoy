from collections.abc import AsyncIterator, Mapping, Sequence
from datetime import datetime
from typing import Any, Protocol

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


class InviteSurface(Protocol):
    async def answer_guest(self, query_id: str, result: Mapping[str, Any]) -> None: ...
    async def answer_inline(self, query_id: str, results: Sequence[Mapping[str, Any]]) -> None: ...


class BotProfile(Protocol):
    async def set_description(self, text: str) -> None: ...


class ManagedBotProfile(BotProfile, UpdateSource, Protocol):
    async def set_name(self, name: str) -> None: ...
    async def close(self) -> None: ...


class BotManager(Protocol):
    async def request_managed_bot(
        self, chat_id: int, request_id: int, name: str, username: str
    ) -> None: ...
    async def managed_bot_token(self, bot_id: int) -> str: ...
    async def replace_managed_bot_token(self, bot_id: int) -> str: ...
    async def set_managed_bot_access(
        self, bot_id: int, restricted: bool, user_ids: Sequence[int]
    ) -> None: ...


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
