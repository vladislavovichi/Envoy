from typing import Any
from uuid import UUID

from envoy.domain.errors import SequenceConflict
from envoy.domain.ids import SessionId
from envoy.domain.journal import JournalEntry
from envoy.journal.ports import JournalStore


class JournalStoreContract:
    async def test_head_of_empty_is_none(self, store: JournalStore) -> None:
        assert await store.head(SessionId(UUID(int=12345))) is None

    async def test_append_then_read_returns_in_order(
        self, store: JournalStore, journal_parts: tuple[Any, ...]
    ) -> None:
        author = journal_parts[-1]
        from envoy.domain.events import JournalEntryType

        entries: list[JournalEntry] = []
        for _ in range(3):
            entry = await author.record(JournalEntryType.decided, b"data", b"commitment")
            await store.append(entry)
            entries.append(entry)
        assert await store.read(entries[0].session_id) == entries

    async def test_duplicate_seq_rejected(
        self, store: JournalStore, journal_parts: tuple[Any, ...]
    ) -> None:
        from envoy.domain.events import JournalEntryType

        entry = await journal_parts[-1].record(JournalEntryType.decided, b"data", b"c")
        await store.append(entry)
        try:
            await store.append(entry)
        except SequenceConflict:
            return
        else:
            raise AssertionError("Duplicate sequence was accepted")

    async def test_read_from_offset(
        self, store: JournalStore, journal_parts: tuple[Any, ...]
    ) -> None:
        from envoy.domain.events import JournalEntryType

        entries = []
        for _ in range(3):
            entry = await journal_parts[-1].record(JournalEntryType.decided, b"data", b"c")
            await store.append(entry)
            entries.append(entry)
        assert await store.read(entries[0].session_id, start=1) == entries[1:]
