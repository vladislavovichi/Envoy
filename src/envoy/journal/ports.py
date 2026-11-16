from typing import Protocol

from envoy.domain.ids import SessionId
from envoy.domain.journal import JournalEntry


class JournalWriter(Protocol):
    async def append(self, entry: JournalEntry) -> None: ...


class JournalCommitter(Protocol):
    async def commit(self, entry: JournalEntry, key: bytes) -> None: ...


class JournalReader(Protocol):
    async def read(self, session: SessionId, start: int = 0) -> list[JournalEntry]: ...
    async def head(self, session: SessionId) -> JournalEntry | None: ...


class KeyVault(Protocol):
    async def put(self, session: SessionId, seq: int, key: bytes) -> None: ...
    async def entry_key(self, session: SessionId, seq: int) -> bytes: ...
    async def erase(self, session: SessionId, seq: int) -> None: ...


class JournalStore(JournalWriter, JournalReader, Protocol):
    pass
