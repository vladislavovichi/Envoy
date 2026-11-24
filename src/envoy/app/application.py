from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from envoy.app.pipeline import Pipeline
from envoy.app.runtime import Runtime
from envoy.crypto.aead import ContentCipher
from envoy.crypto.keys import PublicKey
from envoy.domain.events import JournalEntryType
from envoy.domain.ids import SessionId
from envoy.domain.journal import JournalEntry
from envoy.journal.chain import JournalAuthor
from envoy.journal.ports import JournalReader, KeyVault
from envoy.journal.verifier import JournalVerifier
from envoy.telegram.ports import MessageSender, UpdateSource


@dataclass(frozen=True)
class Application:
    reader: JournalReader
    vault: KeyVault
    cipher: ContentCipher
    public_key: PublicKey
    verifier: JournalVerifier
    sender: MessageSender
    updates: UpdateSource
    pipeline: Pipeline
    runtime: Runtime
    _author: Callable[[SessionId], JournalAuthor]
    _close: Callable[[], Awaitable[None]]

    async def record(
        self, session: SessionId, type: JournalEntryType, payload: bytes, mandate_commitment: bytes
    ) -> JournalEntry:
        return await self._author(session).record(type, payload, mandate_commitment)

    async def close(self) -> None:
        await self._close()
