import asyncio
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass

from envoy.domain.telegram import ReplyMarkup, SentMessage, SurfaceCapabilities, Update
from envoy.telegram.ports import TopicUnavailable


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

    async def bot_username(self) -> str:
        return self._username

    async def updates(self) -> AsyncIterator[Update]:
        while True:
            yield await self.incoming.get()
