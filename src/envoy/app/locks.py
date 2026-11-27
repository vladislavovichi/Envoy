import asyncio
from collections.abc import Hashable

from envoy.app.ports import SessionRecord


class SessionLocks:
    def __init__(self) -> None:
        self._locks: dict[Hashable, asyncio.Lock] = {}

    def for_key(self, key: Hashable) -> asyncio.Lock:
        lock = self._locks.get(key)
        if lock is None:
            lock = self._locks[key] = asyncio.Lock()
        return lock


def session_key(record: SessionRecord) -> Hashable:
    return (record.connection_id, record.counterpart_chat_id)
