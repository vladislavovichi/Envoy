from datetime import date
from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from envoy.app.ports import OwnerTarget, SummaryWidgetRef, WidgetRef
from envoy.domain.ids import OwnerId, SessionId
from envoy.persistence.models import (
    EscalationWidgetRow,
    OwnerSummaryRow,
    OwnerSummaryTopicRow,
    SummaryWidgetRow,
)
from envoy.security.widget_projection import SummaryRow

type Sessions = async_sessionmaker[AsyncSession]


def _summary(row: SummaryWidgetRow) -> SummaryWidgetRef:
    return SummaryWidgetRef(
        OwnerTarget(OwnerId(row.owner_id), row.user_id, row.chat_id, row.thread_id),
        row.day,
        WidgetRef(row.chat_id, row.message_id, row.version, row.revision),
        tuple(
            SummaryRow(str(item["label"]), str(item["status"]), item.get("url"))
            for item in row.rows
        ),
    )


def _rows(rows: tuple[SummaryRow, ...]) -> list[dict[str, Any]]:
    return [{"label": row.label, "status": row.status, "url": row.topic_url} for row in rows]


class PgOwnerViewStore:
    def __init__(self, sessions: Sessions) -> None:
        self._sessions = sessions

    async def put_escalation(self, session: SessionId, cause: int, ref: WidgetRef) -> None:
        values = {
            "session_id": session,
            "cause": cause,
            "chat_id": ref.chat_id,
            "message_id": ref.message_id,
            "version": ref.version,
            "revision": ref.revision,
        }
        statement = insert(EscalationWidgetRow).values(**values)
        async with self._sessions.begin() as db:
            await db.execute(
                statement.on_conflict_do_update(
                    index_elements=[EscalationWidgetRow.session_id, EscalationWidgetRow.cause],
                    set_={
                        key: value
                        for key, value in values.items()
                        if key not in ("session_id", "cause")
                    },
                )
            )

    async def get_escalation(self, session: SessionId, cause: int) -> WidgetRef | None:
        async with self._sessions() as db:
            row = await db.get(EscalationWidgetRow, (session, cause))
            if row is None:
                return None
            return WidgetRef(row.chat_id, row.message_id, row.version, row.revision)

    async def find_escalation(self, chat_id: int, message_id: int) -> tuple[SessionId, int] | None:
        async with self._sessions() as db:
            row = await db.scalar(
                select(EscalationWidgetRow).where(
                    EscalationWidgetRow.chat_id == chat_id,
                    EscalationWidgetRow.message_id == message_id,
                )
            )
            return None if row is None else (SessionId(row.session_id), row.cause)

    async def put_summary(self, summary: SummaryWidgetRef) -> None:
        values = {
            "owner_id": summary.target.owner_id,
            "day": summary.day,
            "user_id": summary.target.user_id,
            "chat_id": summary.ref.chat_id,
            "thread_id": summary.target.thread_id,
            "message_id": summary.ref.message_id,
            "version": summary.ref.version,
            "revision": summary.ref.revision,
            "rows": _rows(summary.rows),
        }
        statement = insert(SummaryWidgetRow).values(**values)
        async with self._sessions.begin() as db:
            await db.execute(
                statement.on_conflict_do_update(
                    index_elements=[SummaryWidgetRow.owner_id, SummaryWidgetRow.day],
                    set_={
                        key: value
                        for key, value in values.items()
                        if key not in ("owner_id", "day")
                    },
                )
            )

    async def get_summary(self, owner: OwnerId, day: date) -> SummaryWidgetRef | None:
        async with self._sessions() as db:
            row = await db.get(SummaryWidgetRow, (owner, day))
            return None if row is None else _summary(row)

    async def find_summary(self, chat_id: int, message_id: int) -> SummaryWidgetRef | None:
        async with self._sessions() as db:
            row = await db.scalar(
                select(SummaryWidgetRow).where(
                    SummaryWidgetRow.chat_id == chat_id, SummaryWidgetRow.message_id == message_id
                )
            )
            return None if row is None else _summary(row)

    async def summary_delivered(self, owner: OwnerId, day: date) -> bool:
        async with self._sessions() as db:
            return await db.get(OwnerSummaryRow, (owner, day)) is not None

    async def mark_summary_delivered(self, owner: OwnerId, day: date) -> None:
        async with self._sessions.begin() as db:
            await db.execute(
                insert(OwnerSummaryRow).values(owner_id=owner, day=day).on_conflict_do_nothing()
            )

    async def summary_topic(self, owner: OwnerId) -> int | None:
        async with self._sessions() as db:
            row = await db.get(OwnerSummaryTopicRow, owner)
            return None if row is None else row.thread_id

    async def set_summary_topic(self, owner: OwnerId, thread_id: int) -> None:
        statement = insert(OwnerSummaryTopicRow).values(owner_id=owner, thread_id=thread_id)
        async with self._sessions.begin() as db:
            await db.execute(
                statement.on_conflict_do_update(
                    index_elements=[OwnerSummaryTopicRow.owner_id], set_={"thread_id": thread_id}
                )
            )
