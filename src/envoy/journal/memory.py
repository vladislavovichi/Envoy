from envoy.domain.errors import ContentErased, SequenceConflict
from envoy.domain.ids import SessionId
from envoy.domain.journal import JournalEntry


class InMemoryJournalStore:
    def __init__(self, vault: InMemoryKeyVault | None = None) -> None:
        self._entries: dict[SessionId, list[JournalEntry]] = {}
        self._vault = vault

    async def commit(self, entry: JournalEntry, key: bytes) -> None:
        if self._vault is None:
            raise ValueError("Atomic journal writes require a bound key vault")
        entries = self._entries.get(entry.session_id, [])
        if entry.seq != len(entries):
            raise SequenceConflict(entry.seq)
        self._vault.put_nowait(entry.session_id, entry.seq, key)
        self._entries.setdefault(entry.session_id, []).append(entry)

    async def append(self, entry: JournalEntry) -> None:
        entries = self._entries.setdefault(entry.session_id, [])
        if entry.seq != len(entries):
            raise SequenceConflict(entry.seq)
        entries.append(entry)

    async def read(self, session: SessionId, start: int = 0) -> list[JournalEntry]:
        return [entry for entry in self._entries.get(session, []) if entry.seq >= start]

    async def head(self, session: SessionId) -> JournalEntry | None:
        entries = self._entries.get(session, [])
        return entries[-1] if entries else None


class InMemoryKeyVault:
    def __init__(self) -> None:
        self._keys: dict[tuple[SessionId, int], bytes] = {}

    async def put(self, session: SessionId, seq: int, key: bytes) -> None:
        self.put_nowait(session, seq, key)

    def put_nowait(self, session: SessionId, seq: int, key: bytes) -> None:
        identity = (session, seq)
        if identity in self._keys:
            raise SequenceConflict(seq)
        self._keys[identity] = key

    async def entry_key(self, session: SessionId, seq: int) -> bytes:
        try:
            return self._keys[(session, seq)]
        except KeyError:
            raise ContentErased("Entry key is unavailable") from None

    async def erase(self, session: SessionId, seq: int) -> None:
        self._keys.pop((session, seq), None)
