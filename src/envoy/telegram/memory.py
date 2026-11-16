import asyncio
from collections.abc import AsyncIterator

from envoy.domain.telegram import ReplyMarkup, SentMessage, SurfaceCapabilities, Update


class FakeTransport:
    capabilities = SurfaceCapabilities(
        private_context=True, ephemeral=False, checklist_native=False
    )

    def __init__(self) -> None:
        self.messages: list[SentMessage] = []
        self.incoming: asyncio.Queue[Update] = asyncio.Queue()

    async def send_message(
        self, chat_id: int, text: str, *, reply_markup: ReplyMarkup | None = None
    ) -> SentMessage:
        message = SentMessage(len(self.messages) + 1, chat_id, text)
        self.messages.append(message)
        return message

    async def updates(self) -> AsyncIterator[Update]:
        while True:
            yield await self.incoming.get()
