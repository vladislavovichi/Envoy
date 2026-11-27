from dataclasses import replace
from datetime import date, datetime

from envoy.app.ports import Job, LedgerStatus, SessionRecord, SummaryWidgetRef, WidgetRef
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

    async def set_dashboard_widget(self, session: SessionId, ref: WidgetRef) -> None:
        self._records[session] = replace(self._records[session], dashboard_widget=ref)

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


class InMemoryOwnerViewStore:
    def __init__(self) -> None:
        self._escalations: dict[tuple[SessionId, int], WidgetRef] = {}
        self._summaries: dict[tuple[OwnerId, date], SummaryWidgetRef] = {}
        self._delivered: set[tuple[OwnerId, date]] = set()
        self._topics: dict[OwnerId, int] = {}

    def _message_taken(self, chat_id: int, message_id: int, key: object) -> bool:
        for other, ref in self._escalations.items():
            if other != key and (ref.chat_id, ref.message_id) == (chat_id, message_id):
                return True
        return False

    async def put_escalation(self, session: SessionId, cause: int, ref: WidgetRef) -> None:
        if self._message_taken(ref.chat_id, ref.message_id, (session, cause)):
            raise ValueError("Message is already bound")
        self._escalations[(session, cause)] = ref

    async def get_escalation(self, session: SessionId, cause: int) -> WidgetRef | None:
        return self._escalations.get((session, cause))

    async def find_escalation(self, chat_id: int, message_id: int) -> tuple[SessionId, int] | None:
        for key, ref in self._escalations.items():
            if (ref.chat_id, ref.message_id) == (chat_id, message_id):
                return key
        return None

    async def put_summary(self, summary: SummaryWidgetRef) -> None:
        self._summaries[(summary.target.owner_id, summary.day)] = summary

    async def get_summary(self, owner: OwnerId, day: date) -> SummaryWidgetRef | None:
        return self._summaries.get((owner, day))

    async def find_summary(self, chat_id: int, message_id: int) -> SummaryWidgetRef | None:
        for summary in self._summaries.values():
            if (summary.ref.chat_id, summary.ref.message_id) == (chat_id, message_id):
                return summary
        return None

    async def summary_delivered(self, owner: OwnerId, day: date) -> bool:
        return (owner, day) in self._delivered

    async def mark_summary_delivered(self, owner: OwnerId, day: date) -> None:
        self._delivered.add((owner, day))

    async def summary_topic(self, owner: OwnerId) -> int | None:
        return self._topics.get(owner)

    async def set_summary_topic(self, owner: OwnerId, thread_id: int) -> None:
        self._topics[owner] = thread_id
