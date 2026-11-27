from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from envoy.domain.ids import AgentId, OwnerId
from envoy.persistence.models import PeerKeyHistoryRow, PeerPinRow
from envoy.protocol.delegation import (
    DelegationError,
    PeerPin,
    RevocationProof,
    RotationProof,
    check_revocation,
    check_rotation,
)

type Sessions = async_sessionmaker[AsyncSession]


def _pin(row: PeerPinRow) -> PeerPin:
    return PeerPin(
        row.bot_id,
        OwnerId(row.owner_id),
        row.owner_key,
        AgentId(row.agent_id),
        row.agent_key,
        row.revision,
        row.revoked,
    )


class PgKeyRegistry:
    def __init__(self, sessions: Sessions) -> None:
        self._sessions = sessions

    async def get(self, bot_id: int) -> PeerPin | None:
        async with self._sessions() as db:
            row = await db.get(PeerPinRow, bot_id)
            return None if row is None else _pin(row)

    async def pin_first(self, pin: PeerPin) -> PeerPin:
        async with self._sessions.begin() as db:
            inserted = await db.scalar(
                insert(PeerPinRow)
                .values(
                    bot_id=pin.bot_id,
                    owner_id=pin.owner_id,
                    owner_key=pin.owner_key,
                    agent_id=pin.agent_id,
                    agent_key=pin.agent_key,
                    revision=pin.revision,
                    revoked=pin.revoked,
                )
                .on_conflict_do_nothing()
                .returning(PeerPinRow.bot_id)
            )
            if inserted is not None:
                db.add(PeerKeyHistoryRow(bot_id=pin.bot_id, position=0, agent_key=pin.agent_key))
                return pin
            row = await db.get(PeerPinRow, pin.bot_id)
            assert row is not None
            return _pin(row)

    async def _locked(self, db: AsyncSession, bot_id: int) -> PeerPinRow:
        row = await db.get(PeerPinRow, bot_id, with_for_update=True)
        if row is None:
            raise DelegationError("not_pinned")
        return row

    async def rotate(self, bot_id: int, proof: RotationProof) -> PeerPin:
        async with self._sessions.begin() as db:
            row = await self._locked(db, bot_id)
            pin = check_rotation(_pin(row), proof)
            row.agent_id = pin.agent_id
            row.agent_key = pin.agent_key
            row.revision = pin.revision
            position = await db.scalar(
                select(func.count())
                .select_from(PeerKeyHistoryRow)
                .where(PeerKeyHistoryRow.bot_id == bot_id)
            )
            db.add(
                PeerKeyHistoryRow(
                    bot_id=bot_id, position=int(position or 0), agent_key=pin.agent_key
                )
            )
            return pin

    async def revoke(self, bot_id: int, proof: RevocationProof) -> None:
        async with self._sessions.begin() as db:
            row = await self._locked(db, bot_id)
            pin = check_revocation(_pin(row), proof)
            row.revision = pin.revision
            row.revoked = True

    async def history(self, bot_id: int) -> tuple[bytes, ...]:
        async with self._sessions() as db:
            rows = await db.scalars(
                select(PeerKeyHistoryRow.agent_key)
                .where(PeerKeyHistoryRow.bot_id == bot_id)
                .order_by(PeerKeyHistoryRow.position)
            )
            return tuple(rows)
