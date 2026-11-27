from typing import cast

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from envoy.app.managed_bot_flow import ManagedBotRecord, ManagedStatus
from envoy.domain.ids import OwnerId
from envoy.persistence.models import ManagedBotRow

type Sessions = async_sessionmaker[AsyncSession]


def _record(row: ManagedBotRow) -> ManagedBotRecord:
    return ManagedBotRecord(
        owner=OwnerId(row.owner_id),
        owner_user=row.owner_user,
        request_id=row.request_id,
        secret=row.secret,
        status=cast(ManagedStatus, row.status),
        bot_id=row.bot_id,
        username=row.username,
        token=row.token,
        trusted=tuple(int(user) for user in row.trusted),
    )


class PgManagedBotRepository:
    def __init__(self, sessions: Sessions) -> None:
        self._sessions = sessions

    async def put(self, record: ManagedBotRecord) -> None:
        values = {
            "owner_id": record.owner,
            "request_id": record.request_id,
            "owner_user": record.owner_user,
            "secret": record.secret,
            "status": record.status,
            "bot_id": record.bot_id,
            "username": record.username,
            "token": record.token,
            "trusted": list(record.trusted),
        }
        statement = insert(ManagedBotRow).values(**values)
        async with self._sessions.begin() as db:
            await db.execute(
                statement.on_conflict_do_update(
                    index_elements=[ManagedBotRow.owner_id, ManagedBotRow.request_id],
                    set_={
                        key: value
                        for key, value in values.items()
                        if key not in ("owner_id", "request_id")
                    },
                )
            )

    async def pending(self, owner: OwnerId) -> ManagedBotRecord | None:
        async with self._sessions() as db:
            row = await db.scalar(
                select(ManagedBotRow)
                .where(ManagedBotRow.owner_id == owner, ManagedBotRow.status == "requested")
                .order_by(ManagedBotRow.request_id.desc())
                .limit(1)
            )
            return None if row is None else _record(row)

    async def by_bot(self, bot_id: int) -> ManagedBotRecord | None:
        async with self._sessions() as db:
            row = await db.scalar(select(ManagedBotRow).where(ManagedBotRow.bot_id == bot_id))
            return None if row is None else _record(row)

    async def all(self) -> list[ManagedBotRecord]:
        async with self._sessions() as db:
            return [_record(row) for row in await db.scalars(select(ManagedBotRow))]
