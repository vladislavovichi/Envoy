from dataclasses import replace
from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from envoy.app.agent_ports import (
    AgentSession,
    Inbound,
    StaleSession,
    decode_session,
    encode_session,
)
from envoy.crypto.aead import ContentCipher
from envoy.domain.ids import OwnerId, SessionId
from envoy.persistence.models import (
    ProtocolEvidenceRow,
    ProtocolNonceRow,
    ProtocolSessionRow,
    ProtocolUpdateRow,
)

type Sessions = async_sessionmaker[AsyncSession]


class PgProtocolRepository:
    def __init__(self, sessions: Sessions, cipher: ContentCipher, master_key: bytes) -> None:
        self._sessions = sessions
        self._cipher = cipher
        self._key = master_key

    def _decode(self, row: ProtocolSessionRow) -> AgentSession:
        return decode_session(self._cipher.decrypt(self._key, row.body_ciphertext, row.id.bytes))

    async def get(self, id: SessionId) -> AgentSession | None:
        async with self._sessions() as db:
            row = await db.get(ProtocolSessionRow, id)
            return None if row is None else self._decode(row)

    async def active_with(self, bot_id: int, peer_bot_id: int) -> list[AgentSession]:
        async with self._sessions() as db:
            rows = await db.scalars(
                select(ProtocolSessionRow).where(
                    ProtocolSessionRow.bot_id == bot_id,
                    ProtocolSessionRow.peer_bot_id == peer_bot_id,
                    ProtocolSessionRow.closed.is_(False),
                )
            )
            return [self._decode(row) for row in rows]

    async def seen(self, bot_id: int, update_id: int) -> bool:
        async with self._sessions() as db:
            return await db.get(ProtocolUpdateRow, (bot_id, update_id)) is not None

    async def nonce_seen(self, sender: int, nonce: bytes, now: datetime) -> bool:
        async with self._sessions() as db:
            row = await db.get(ProtocolNonceRow, (sender, nonce))
            return row is not None and row.expires_at > now

    async def _inbound(self, db: AsyncSession, inbound: Inbound, session: SessionId | None) -> None:
        await db.execute(
            insert(ProtocolUpdateRow)
            .values(bot_id=inbound.bot_id, update_id=inbound.update_id)
            .on_conflict_do_nothing()
        )
        if inbound.nonce is not None and inbound.nonce_expires is not None:
            statement = insert(ProtocolNonceRow).values(
                sender=inbound.sender, nonce=inbound.nonce, expires_at=inbound.nonce_expires
            )
            await db.execute(
                statement.on_conflict_do_update(
                    index_elements=[ProtocolNonceRow.sender, ProtocolNonceRow.nonce],
                    set_={"expires_at": statement.excluded.expires_at},
                )
            )
        if inbound.text is not None and session is not None:
            aad = session.bytes + inbound.update_id.to_bytes(8, "big", signed=True)
            await db.execute(
                insert(ProtocolEvidenceRow)
                .values(
                    session_id=session,
                    bot_id=inbound.bot_id,
                    update_id=inbound.update_id,
                    text_ciphertext=self._cipher.encrypt(
                        self._key, inbound.text.encode("utf-8"), aad
                    ),
                )
                .on_conflict_do_nothing()
            )

    async def mark(self, inbound: Inbound) -> None:
        async with self._sessions.begin() as db:
            await self._inbound(db, inbound, None)

    async def save(self, session: AgentSession, *, inbound: Inbound | None = None) -> AgentSession:
        saved = replace(session, version=session.version + 1)
        body = self._cipher.encrypt(self._key, encode_session(saved), session.id.bytes)
        async with self._sessions.begin() as db:
            row = await db.get(ProtocolSessionRow, session.id, with_for_update=True)
            stored = 0 if row is None else row.version
            if stored != session.version:
                raise StaleSession(str(session.id))
            values = {
                "bot_id": saved.bot_id,
                "peer_bot_id": saved.peer_bot_id,
                "owner_id": saved.owner_id,
                "closed": saved.closed,
                "agreed_at": saved.agreed_at,
                "next_due": saved.next_due(),
                "version": saved.version,
                "body_ciphertext": body,
            }
            if row is None:
                db.add(ProtocolSessionRow(id=saved.id, **values))
            else:
                for key, value in values.items():
                    setattr(row, key, value)
            if inbound is not None:
                await db.flush()
                await self._inbound(db, inbound, saved.id)
        return saved

    async def due(self, now: datetime) -> list[AgentSession]:
        async with self._sessions() as db:
            rows = await db.scalars(
                select(ProtocolSessionRow)
                .where(ProtocolSessionRow.next_due.is_not(None), ProtocolSessionRow.next_due <= now)
                .order_by(ProtocolSessionRow.next_due, ProtocolSessionRow.id)
            )
            return [self._decode(row) for row in rows]

    async def count_agreed_since(self, owner: OwnerId, since: datetime) -> int:
        async with self._sessions() as db:
            count = await db.scalar(
                select(func.count())
                .select_from(ProtocolSessionRow)
                .where(ProtocolSessionRow.owner_id == owner, ProtocolSessionRow.agreed_at >= since)
            )
            return int(count or 0)

    async def evidence(self, session: SessionId) -> list[str]:
        async with self._sessions() as db:
            rows = await db.scalars(
                select(ProtocolEvidenceRow)
                .where(ProtocolEvidenceRow.session_id == session)
                .order_by(ProtocolEvidenceRow.bot_id, ProtocolEvidenceRow.update_id)
            )
            return [
                self._cipher.decrypt(
                    self._key,
                    row.text_ciphertext,
                    session.bytes + row.update_id.to_bytes(8, "big", signed=True),
                ).decode("utf-8")
                for row in rows
            ]
