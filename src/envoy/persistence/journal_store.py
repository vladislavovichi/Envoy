from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from envoy.domain.errors import SequenceConflict
from envoy.domain.ids import SessionId
from envoy.domain.journal import JournalEntry
from envoy.persistence.key_vault import PgKeyVault
from envoy.persistence.models import EntryKeyRow, JournalRow


class PgJournalStore:
    def __init__(
        self, session_factory: async_sessionmaker[AsyncSession], vault: PgKeyVault | None = None
    ) -> None:
        self._sessions = session_factory
        self._vault = vault

    async def append(self, entry: JournalEntry) -> None:
        await self._write(entry)

    async def commit(self, entry: JournalEntry, key: bytes) -> None:
        if self._vault is None:
            raise ValueError("Atomic journal writes require a bound key vault")
        wrapped = self._vault.wrap_entry_key(entry.session_id, entry.seq, key)
        await self._write(entry, wrapped)

    async def _write(self, entry: JournalEntry, wrapped_key: bytes | None = None) -> None:
        try:
            async with self._sessions.begin() as db:
                previous = await db.scalar(
                    select(func.max(JournalRow.seq)).where(
                        JournalRow.session_id == entry.session_id
                    )
                )
                if entry.seq != (0 if previous is None else previous + 1):
                    raise SequenceConflict(entry.seq)
                db.add(JournalRow(**entry.model_dump()))
                if wrapped_key is not None:
                    db.add(
                        EntryKeyRow(
                            session_id=entry.session_id, seq=entry.seq, wrapped_key=wrapped_key
                        )
                    )
        except IntegrityError as exc:
            if getattr(exc.orig, "sqlstate", None) == "23505":
                raise SequenceConflict(entry.seq) from None
            raise

    async def read(self, session: SessionId, start: int = 0) -> list[JournalEntry]:
        async with self._sessions() as db:
            rows = await db.scalars(
                select(JournalRow)
                .where(JournalRow.session_id == session, JournalRow.seq >= start)
                .order_by(JournalRow.seq)
            )
            return [JournalEntry.model_validate(row, from_attributes=True) for row in rows]

    async def head(self, session: SessionId) -> JournalEntry | None:
        async with self._sessions() as db:
            row = await db.scalar(
                select(JournalRow)
                .where(JournalRow.session_id == session)
                .order_by(JournalRow.seq.desc())
                .limit(1)
            )
            return None if row is None else JournalEntry.model_validate(row, from_attributes=True)
