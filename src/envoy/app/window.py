from datetime import timedelta

from envoy.app.ports import BusinessConnectionRepository
from envoy.ports.clock import Clock

REPLY_WINDOW = timedelta(hours=24)


class ReplyWindow:
    def __init__(self, connections: BusinessConnectionRepository, clock: Clock) -> None:
        self._connections = connections
        self._clock = clock

    async def record_incoming(self, connection_id: str, chat_id: int) -> None:
        await self._connections.touch_chat(connection_id, chat_id, self._clock.now())

    async def is_open(self, connection_id: str, chat_id: int) -> bool:
        last = await self._connections.last_incoming_at(connection_id, chat_id)
        return last is not None and self._clock.now() - last <= REPLY_WINDOW
