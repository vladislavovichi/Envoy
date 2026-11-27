import asyncio
from collections.abc import AsyncIterator, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import datetime
from typing import Any

from envoy.domain.telegram import ReplyMarkup, SentMessage, SurfaceCapabilities, Update
from envoy.telegram.ports import TopicUnavailable, WidgetRejected


@dataclass(frozen=True)
class SentRecord:
    message: SentMessage
    reply_markup: ReplyMarkup | None
    business_connection_id: str | None
    thread_id: int | None


@dataclass(frozen=True)
class ChecklistRecord:
    message: SentMessage
    business_connection_id: str
    title: str
    items: tuple[str, ...]


@dataclass(frozen=True)
class WidgetRecord:
    message: SentMessage
    html: str
    state: str
    caption: str
    thread_id: int | None
    allow_queries: bool
    reply_markup: ReplyMarkup | None


class FakeTransport:
    capabilities = SurfaceCapabilities(private_context=True, ephemeral=False, checklist_native=True)

    def __init__(self, username: str = "envoy_bot", topics_available: bool = True) -> None:
        self.messages: list[SentMessage] = []
        self.sent: list[SentRecord] = []
        self.checklists: list[ChecklistRecord] = []
        self.topics: list[tuple[int, str, int]] = []
        self.callback_answers: list[tuple[str, str | None]] = []
        self.incoming: asyncio.Queue[Update] = asyncio.Queue()
        self.topics_available = topics_available
        self.widgets: dict[int, WidgetRecord] = {}
        self.widget_edits: list[tuple[int, int, str]] = []
        self.widget_answers: list[tuple[str, str]] = []
        self.widget_states: dict[int, list[str]] = {}
        self.widgets_available = True
        self.descriptions: list[str] = []
        self.guest_answers: list[tuple[str, dict[str, Any]]] = []
        self.inline_answers: list[tuple[str, list[dict[str, Any]]]] = []
        self._username = username

    def _next_message(self, chat_id: int, text: str) -> SentMessage:
        message = SentMessage(len(self.messages) + 1, chat_id, text)
        self.messages.append(message)
        return message

    async def send_message(
        self,
        chat_id: int,
        text: str,
        *,
        reply_markup: ReplyMarkup | None = None,
        business_connection_id: str | None = None,
        thread_id: int | None = None,
    ) -> SentMessage:
        message = self._next_message(chat_id, text)
        self.sent.append(SentRecord(message, reply_markup, business_connection_id, thread_id))
        return message

    async def create_topic(self, chat_id: int, title: str) -> int:
        if not self.topics_available:
            raise TopicUnavailable("Topics are disabled")
        thread_id = 1000 + len(self.topics)
        self.topics.append((chat_id, title, thread_id))
        return thread_id

    async def send_checklist(
        self, chat_id: int, business_connection_id: str, title: str, items: Sequence[str]
    ) -> SentMessage:
        message = self._next_message(chat_id, title)
        self.checklists.append(
            ChecklistRecord(message, business_connection_id, title, tuple(items))
        )
        return message

    async def answer_callback(self, callback_id: str, text: str | None = None) -> None:
        self.callback_answers.append((callback_id, text))

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
    ) -> int:
        if not self.widgets_available or len(state.encode()) > 1024 or len(caption) > 1024:
            raise WidgetRejected("Widget rejected")
        message = self._next_message(chat_id, caption)
        self.widgets[message.message_id] = WidgetRecord(
            message, html, state, caption, thread_id, allow_queries, reply_markup
        )
        self.widget_states[message.message_id] = [state]
        return message.message_id

    async def edit_widget(self, chat_id: int, message_id: int, state: str) -> None:
        record = self.widgets.get(message_id)
        if record is None or record.message.chat_id != chat_id or len(state.encode()) > 1024:
            raise WidgetRejected("Widget edit rejected")
        if record.state == state:
            return
        self.widgets[message_id] = replace(record, state=state)
        self.widget_edits.append((chat_id, message_id, state))
        self.widget_states[message_id].append(state)

    async def answer_widget(self, query_id: str, data: str, *, deadline: datetime) -> None:
        self.widget_answers.append((query_id, data))

    async def answer_guest(self, query_id: str, result: Mapping[str, Any]) -> None:
        self.guest_answers.append((query_id, dict(result)))

    async def answer_inline(self, query_id: str, results: Sequence[Mapping[str, Any]]) -> None:
        self.inline_answers.append((query_id, [dict(result) for result in results]))

    async def set_description(self, text: str) -> None:
        self.descriptions.append(text)

    async def bot_username(self) -> str:
        return self._username

    async def updates(self) -> AsyncIterator[Update]:
        while True:
            yield await self.incoming.get()
