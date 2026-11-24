from collections.abc import Callable

from envoy.crypto.aead import ContentCipher
from envoy.domain.events import SessionEvent, decode_event, encode_event
from envoy.domain.ids import SessionId
from envoy.domain.journal import JournalEntry
from envoy.journal.chain import JournalAuthor
from envoy.journal.ports import JournalReader, KeyVault
from envoy.journal.verifier import JournalVerifier


class SessionJournal:
    def __init__(
        self,
        author: Callable[[SessionId], JournalAuthor],
        reader: JournalReader,
        vault: KeyVault,
        cipher: ContentCipher,
    ) -> None:
        self._author = author
        self._reader = reader
        self._vault = vault
        self._cipher = cipher
        self._verifier = JournalVerifier()

    async def append(
        self, session: SessionId, event: SessionEvent, mandate_commitment: bytes
    ) -> JournalEntry:
        entry_type, payload = encode_event(event)
        return await self._author(session).record(entry_type, payload, mandate_commitment)

    async def entries(self, session: SessionId) -> list[JournalEntry]:
        return await self._reader.read(session)

    async def events(self, session: SessionId) -> list[SessionEvent]:
        events: list[SessionEvent] = []
        for entry in await self._reader.read(session):
            payload = await self._verifier.read_payload(entry, self._vault, self._cipher)
            events.append(decode_event(entry.type, payload))
        return events
