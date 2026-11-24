from dataclasses import replace
from datetime import datetime

from envoy.app.ports import Job, LedgerStatus, SessionRecord
from envoy.domain.ids import OwnerId, SessionId
from envoy.domain.telegram import BusinessConnection


class InMemorySessionRepository:
    def __init__(self) -> None:
        self._records: dict[SessionId, SessionRecord] = {}

    async def create(self, record: SessionRecord) -> None:
        if record.id in self._records:
            raise ValueError("Session already exists")
        self._records[record.id] = record

    async def get(self, id: SessionId) -> SessionRecord | None:
        return self._records.get(id)

    async def find_active(self, connection_id: str, chat_id: int) -> SessionRecord | None:
        for record in self._records.values():
            if (
                record.closed_at is None
                and record.connection_id == connection_id
                and record.counterpart_chat_id == chat_id
            ):
                return record
        return None

    async def list_active(self) -> list[SessionRecord]:
        return [record for record in self._records.values() if record.closed_at is None]

    async def mark_closed(self, id: SessionId, at: datetime) -> None:
        record = self._records[id]
        if record.closed_at is None:
            self._records[id] = replace(record, closed_at=at)

    async def mark_agreed(self, id: SessionId, at: datetime) -> None:
        record = self._records[id]
        if record.agreed_at is None:
            self._records[id] = replace(record, agreed_at=at)

    async def count_agreed_since(self, owner: OwnerId, since: datetime) -> int:
        return sum(
            1
            for record in self._records.values()
            if record.owner_id == owner
            and record.agreed_at is not None
            and record.agreed_at >= since
        )


class InMemoryBusinessConnectionRepository:
    def __init__(self) -> None:
        self._connections: dict[str, BusinessConnection] = {}
        self._chats: dict[tuple[str, int], datetime] = {}

    async def upsert(self, connection: BusinessConnection) -> None:
        self._connections[connection.id] = connection

    async def get(self, id: str) -> BusinessConnection | None:
        return self._connections.get(id)

    async def touch_chat(self, connection_id: str, chat_id: int, at: datetime) -> None:
        previous = self._chats.get((connection_id, chat_id))
        if previous is None or at > previous:
            self._chats[(connection_id, chat_id)] = at

    async def last_incoming_at(self, connection_id: str, chat_id: int) -> datetime | None:
        return self._chats.get((connection_id, chat_id))


class InMemoryUpdateLedger:
    def __init__(self) -> None:
        self._status: dict[int, LedgerStatus] = {}

    async def begin(self, update_id: int) -> LedgerStatus:
        current = self._status.get(update_id)
        if current is None:
            self._status[update_id] = "in_progress"
            return "new"
        return current

    async def complete(self, update_id: int) -> None:
        self._status[update_id] = "done"


class InMemoryScheduler:
    def __init__(self) -> None:
        self._jobs: dict[str, Job] = {}
        self._done: set[str] = set()

    async def schedule(self, job: Job) -> None:
        if job.key not in self._jobs and job.key not in self._done:
            self._jobs[job.key] = job

    async def cancel(self, key: str) -> None:
        self._jobs.pop(key, None)

    async def due(self, now: datetime) -> list[Job]:
        return sorted(
            (job for job in self._jobs.values() if job.run_at <= now),
            key=lambda job: (job.run_at, job.key),
        )

    async def complete(self, key: str) -> None:
        self._jobs.pop(key, None)
        self._done.add(key)

    async def pending(self, session: SessionId) -> list[Job]:
        return sorted(
            (job for job in self._jobs.values() if job.session == session),
            key=lambda job: (job.run_at, job.key),
        )
