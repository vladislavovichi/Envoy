from collections.abc import Mapping
from dataclasses import replace
from datetime import date, datetime
from types import MappingProxyType
from typing import cast

from sqlalchemy import delete, func, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from envoy.app.ports import Job, JobKind, LedgerStatus, SessionRecord, WidgetRef
from envoy.crypto.aead import ContentCipher
from envoy.domain.ids import MandateId, OwnerId, SessionId
from envoy.domain.telegram import BusinessConnection
from envoy.mandate.ports import MandateNotFound, MandateStateError, MandateStatus, StoredMandate
from envoy.mandate.schema import Mandate
from envoy.persistence.models import (
    BusinessChatRow,
    BusinessConnectionRow,
    LlmUsageRow,
    MandateRow,
    ProcessedUpdateRow,
    ScheduledJobRow,
    SessionRow,
)
from envoy.ports.ids import IdGenerator

type Sessions = async_sessionmaker[AsyncSession]


class PgMandateStore:
    def __init__(
        self, sessions: Sessions, ids: IdGenerator, cipher: ContentCipher, master_key: bytes
    ) -> None:
        self._sessions = sessions
        self._ids = ids
        self._cipher = cipher
        self._key = master_key

    def _record(self, row: MandateRow) -> StoredMandate:
        body = self._cipher.decrypt(self._key, row.body_ciphertext, row.id.bytes)
        return StoredMandate(
            MandateId(row.id),
            OwnerId(row.owner_id),
            row.version,
            Mandate.model_validate_json(body),
            row.salt,
            cast(MandateStatus, row.status),
            row.signature,
        )

    async def save_draft(self, owner: OwnerId, mandate: Mandate, salt: bytes) -> StoredMandate:
        identity = MandateId(self._ids.new_id())
        async with self._sessions.begin() as db:
            await db.execute(select(func.pg_advisory_xact_lock(func.hashtext(str(owner)))))
            latest = await db.scalar(
                select(func.max(MandateRow.version)).where(MandateRow.owner_id == owner)
            )
            version = 1 + (latest or 0)
            versioned = mandate.model_copy(update={"version": version})
            body = versioned.model_dump_json().encode()
            db.add(
                MandateRow(
                    id=identity,
                    owner_id=owner,
                    version=version,
                    body_ciphertext=self._cipher.encrypt(self._key, body, identity.bytes),
                    salt=salt,
                    status="draft",
                    signature=None,
                )
            )
        return StoredMandate(identity, owner, version, versioned, salt, "draft")

    async def get(self, mandate_id: MandateId) -> StoredMandate:
        async with self._sessions() as db:
            row = await db.get(MandateRow, mandate_id)
            if row is None:
                raise MandateNotFound(str(mandate_id))
            return self._record(row)

    async def _transition(
        self, mandate_id: MandateId, status: MandateStatus, signature: bytes | None
    ) -> StoredMandate:
        async with self._sessions.begin() as db:
            row = await db.get(MandateRow, mandate_id, with_for_update=True)
            if row is None:
                raise MandateNotFound(str(mandate_id))
            if row.status != "draft":
                raise MandateStateError(f"Mandate is {row.status}")
            if status == "active":
                await db.execute(
                    update(MandateRow)
                    .where(MandateRow.owner_id == row.owner_id, MandateRow.status == "active")
                    .values(status="superseded")
                )
                row.signature = signature
            row.status = status
            record = self._record(row)
        return replace(record, status=status, signature=signature)

    async def activate(self, mandate_id: MandateId, signature: bytes) -> StoredMandate:
        return await self._transition(mandate_id, "active", signature)

    async def discard(self, mandate_id: MandateId) -> StoredMandate:
        return await self._transition(mandate_id, "discarded", None)

    async def active(self, owner: OwnerId) -> StoredMandate | None:
        async with self._sessions() as db:
            row = await db.scalar(
                select(MandateRow).where(
                    MandateRow.owner_id == owner, MandateRow.status == "active"
                )
            )
            return None if row is None else self._record(row)


def _session(row: SessionRow) -> SessionRecord:
    return SessionRecord(
        id=SessionId(row.id),
        owner_id=OwnerId(row.owner_id),
        mandate_id=MandateId(row.mandate_id),
        connection_id=row.connection_id,
        counterpart_chat_id=row.counterpart_chat_id,
        owner_chat_id=row.owner_chat_id,
        topic_id=row.topic_id,
        deadline=row.deadline,
        closed_at=row.closed_at,
        agreed_at=row.agreed_at,
        dashboard_widget=_dashboard(row),
    )


def _dashboard(row: SessionRow) -> WidgetRef | None:
    if (
        row.dashboard_chat_id is None
        or row.dashboard_message_id is None
        or row.dashboard_version is None
        or row.dashboard_revision is None
    ):
        return None
    return WidgetRef(
        row.dashboard_chat_id,
        row.dashboard_message_id,
        row.dashboard_version,
        row.dashboard_revision,
    )


class PgSessionRepository:
    def __init__(self, sessions: Sessions) -> None:
        self._sessions = sessions

    async def create(self, record: SessionRecord) -> None:
        async with self._sessions.begin() as db:
            db.add(
                SessionRow(
                    id=record.id,
                    owner_id=record.owner_id,
                    mandate_id=record.mandate_id,
                    connection_id=record.connection_id,
                    counterpart_chat_id=record.counterpart_chat_id,
                    owner_chat_id=record.owner_chat_id,
                    topic_id=record.topic_id,
                    deadline=record.deadline,
                    closed_at=record.closed_at,
                    agreed_at=record.agreed_at,
                )
            )

    async def get(self, id: SessionId) -> SessionRecord | None:
        async with self._sessions() as db:
            row = await db.get(SessionRow, id)
            return None if row is None else _session(row)

    async def find_active(self, connection_id: str, chat_id: int) -> SessionRecord | None:
        async with self._sessions() as db:
            row = await db.scalar(
                select(SessionRow)
                .where(
                    SessionRow.connection_id == connection_id,
                    SessionRow.counterpart_chat_id == chat_id,
                    SessionRow.closed_at.is_(None),
                )
                .limit(1)
            )
            return None if row is None else _session(row)

    async def list_active(self) -> list[SessionRecord]:
        async with self._sessions() as db:
            rows = await db.scalars(select(SessionRow).where(SessionRow.closed_at.is_(None)))
            return [_session(row) for row in rows]

    async def mark_closed(self, id: SessionId, at: datetime) -> None:
        async with self._sessions.begin() as db:
            await db.execute(
                update(SessionRow)
                .where(SessionRow.id == id, SessionRow.closed_at.is_(None))
                .values(closed_at=at)
            )

    async def mark_agreed(self, id: SessionId, at: datetime) -> None:
        async with self._sessions.begin() as db:
            await db.execute(
                update(SessionRow)
                .where(SessionRow.id == id, SessionRow.agreed_at.is_(None))
                .values(agreed_at=at)
            )

    async def set_dashboard_widget(self, session: SessionId, ref: WidgetRef) -> None:
        async with self._sessions.begin() as db:
            await db.execute(
                update(SessionRow)
                .where(SessionRow.id == session)
                .values(
                    dashboard_chat_id=ref.chat_id,
                    dashboard_message_id=ref.message_id,
                    dashboard_version=ref.version,
                    dashboard_revision=ref.revision,
                )
            )

    async def count_agreed_since(self, owner: OwnerId, since: datetime) -> int:
        async with self._sessions() as db:
            count = await db.scalar(
                select(func.count())
                .select_from(SessionRow)
                .where(SessionRow.owner_id == owner, SessionRow.agreed_at >= since)
            )
            return int(count or 0)


class PgBusinessConnectionRepository:
    def __init__(self, sessions: Sessions) -> None:
        self._sessions = sessions

    async def upsert(self, connection: BusinessConnection) -> None:
        values = {
            "id": connection.id,
            "owner_user_id": connection.owner_user_id,
            "user_chat_id": connection.user_chat_id,
            "rights": dict(connection.rights),
            "enabled": connection.enabled,
        }
        statement = insert(BusinessConnectionRow).values(**values)
        async with self._sessions.begin() as db:
            await db.execute(
                statement.on_conflict_do_update(
                    index_elements=[BusinessConnectionRow.id],
                    set_={key: value for key, value in values.items() if key != "id"},
                )
            )

    async def get(self, id: str) -> BusinessConnection | None:
        async with self._sessions() as db:
            row = await db.get(BusinessConnectionRow, id)
            if row is None:
                return None
            return BusinessConnection(
                row.id,
                row.owner_user_id,
                row.user_chat_id,
                MappingProxyType(dict(row.rights)),
                row.enabled,
            )

    async def touch_chat(self, connection_id: str, chat_id: int, at: datetime) -> None:
        statement = insert(BusinessChatRow).values(
            connection_id=connection_id, chat_id=chat_id, last_incoming_at=at
        )
        async with self._sessions.begin() as db:
            await db.execute(
                statement.on_conflict_do_update(
                    index_elements=[BusinessChatRow.connection_id, BusinessChatRow.chat_id],
                    set_={
                        "last_incoming_at": func.greatest(
                            BusinessChatRow.last_incoming_at, statement.excluded.last_incoming_at
                        )
                    },
                )
            )

    async def last_incoming_at(self, connection_id: str, chat_id: int) -> datetime | None:
        async with self._sessions() as db:
            row = await db.get(BusinessChatRow, (connection_id, chat_id))
            return None if row is None else row.last_incoming_at


class PgUpdateLedger:
    def __init__(self, sessions: Sessions) -> None:
        self._sessions = sessions

    async def begin(self, update_id: int) -> LedgerStatus:
        async with self._sessions.begin() as db:
            inserted = await db.scalar(
                insert(ProcessedUpdateRow)
                .values(update_id=update_id, status="in_progress")
                .on_conflict_do_nothing()
                .returning(ProcessedUpdateRow.update_id)
            )
            if inserted is not None:
                return "new"
            status = await db.scalar(
                select(ProcessedUpdateRow.status).where(ProcessedUpdateRow.update_id == update_id)
            )
            return cast(LedgerStatus, status)

    async def complete(self, update_id: int) -> None:
        statement = insert(ProcessedUpdateRow).values(update_id=update_id, status="done")
        async with self._sessions.begin() as db:
            await db.execute(
                statement.on_conflict_do_update(
                    index_elements=[ProcessedUpdateRow.update_id], set_={"status": "done"}
                )
            )


def _job(row: ScheduledJobRow) -> Job:
    return Job(
        key=row.key,
        kind=cast(JobKind, row.kind),
        session=SessionId(row.session_id),
        run_at=row.run_at,
        payload=MappingProxyType(dict(row.payload)),
    )


class PgScheduler:
    def __init__(self, sessions: Sessions) -> None:
        self._sessions = sessions

    async def schedule(self, job: Job) -> None:
        payload: Mapping[str, object] = dict(job.payload)
        async with self._sessions.begin() as db:
            await db.execute(
                insert(ScheduledJobRow)
                .values(
                    key=job.key,
                    kind=job.kind,
                    session_id=job.session,
                    run_at=job.run_at,
                    payload=payload,
                    status="pending",
                )
                .on_conflict_do_nothing()
            )

    async def cancel(self, key: str) -> None:
        async with self._sessions.begin() as db:
            await db.execute(
                delete(ScheduledJobRow).where(
                    ScheduledJobRow.key == key, ScheduledJobRow.status == "pending"
                )
            )

    async def due(self, now: datetime) -> list[Job]:
        async with self._sessions() as db:
            rows = await db.scalars(
                select(ScheduledJobRow)
                .where(ScheduledJobRow.status == "pending", ScheduledJobRow.run_at <= now)
                .order_by(ScheduledJobRow.run_at, ScheduledJobRow.key)
            )
            return [_job(row) for row in rows]

    async def complete(self, key: str) -> None:
        async with self._sessions.begin() as db:
            await db.execute(
                update(ScheduledJobRow).where(ScheduledJobRow.key == key).values(status="done")
            )

    async def pending(self, session: SessionId) -> list[Job]:
        async with self._sessions() as db:
            rows = await db.scalars(
                select(ScheduledJobRow)
                .where(ScheduledJobRow.session_id == session, ScheduledJobRow.status == "pending")
                .order_by(ScheduledJobRow.run_at, ScheduledJobRow.key)
            )
            return [_job(row) for row in rows]


class PgUsageLedger:
    def __init__(self, sessions: Sessions) -> None:
        self._sessions = sessions

    async def add(self, day: date, tokens: int) -> int:
        statement = insert(LlmUsageRow).values(day=day, tokens=tokens)
        async with self._sessions.begin() as db:
            total = await db.scalar(
                statement.on_conflict_do_update(
                    index_elements=[LlmUsageRow.day],
                    set_={"tokens": LlmUsageRow.tokens + statement.excluded.tokens},
                ).returning(LlmUsageRow.tokens)
            )
            return int(total or 0)
