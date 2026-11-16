from sqlalchemy import delete
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from envoy.crypto.aead import ContentCipher
from envoy.domain.errors import ContentErased, SequenceConflict
from envoy.domain.ids import SessionId
from envoy.journal.hashing import associated_data
from envoy.persistence.models import EntryKeyRow


class PgKeyVault:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        master_key: bytes,
        cipher: ContentCipher,
    ) -> None:
        self._sessions = session_factory
        self._master_key = master_key
        self._cipher = cipher

    async def put(self, session: SessionId, seq: int, key: bytes) -> None:
        wrapped = self.wrap_entry_key(session, seq, key)
        try:
            async with self._sessions.begin() as db:
                db.add(EntryKeyRow(session_id=session, seq=seq, wrapped_key=wrapped))
        except IntegrityError as exc:
            if getattr(exc.orig, "sqlstate", None) == "23505":
                raise SequenceConflict(seq) from None
            raise

    def wrap_entry_key(self, session: SessionId, seq: int, key: bytes) -> bytes:
        return self._cipher.encrypt(self._master_key, key, associated_data(session, seq))

    async def entry_key(self, session: SessionId, seq: int) -> bytes:
        async with self._sessions() as db:
            row = await db.get(EntryKeyRow, (session, seq))
            if row is None:
                raise ContentErased("Entry key is unavailable")
            return self._cipher.decrypt(
                self._master_key, row.wrapped_key, associated_data(session, seq)
            )

    async def erase(self, session: SessionId, seq: int) -> None:
        async with self._sessions.begin() as db:
            await db.execute(
                delete(EntryKeyRow).where(EntryKeyRow.session_id == session, EntryKeyRow.seq == seq)
            )
