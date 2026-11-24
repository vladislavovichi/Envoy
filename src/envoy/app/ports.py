from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from types import MappingProxyType
from typing import Literal, Protocol

from pydantic import JsonValue

from envoy.domain.ids import MandateId, OwnerId, SessionId
from envoy.domain.telegram import BusinessConnection

type LedgerStatus = Literal["new", "in_progress", "done"]
type JobKind = Literal["escalation_timeout", "session_deadline", "commitment_reminder"]


@dataclass(frozen=True)
class SessionRecord:
    id: SessionId
    owner_id: OwnerId
    mandate_id: MandateId
    connection_id: str
    counterpart_chat_id: int
    owner_chat_id: int
    topic_id: int | None
    deadline: datetime
    closed_at: datetime | None = None
    agreed_at: datetime | None = None


class SessionRepository(Protocol):
    async def create(self, record: SessionRecord) -> None: ...
    async def get(self, id: SessionId) -> SessionRecord | None: ...
    async def find_active(self, connection_id: str, chat_id: int) -> SessionRecord | None: ...
    async def list_active(self) -> list[SessionRecord]: ...
    async def mark_closed(self, id: SessionId, at: datetime) -> None: ...
    async def mark_agreed(self, id: SessionId, at: datetime) -> None: ...
    async def count_agreed_since(self, owner: OwnerId, since: datetime) -> int: ...


class BusinessConnectionRepository(Protocol):
    async def upsert(self, connection: BusinessConnection) -> None: ...
    async def get(self, id: str) -> BusinessConnection | None: ...
    async def touch_chat(self, connection_id: str, chat_id: int, at: datetime) -> None: ...
    async def last_incoming_at(self, connection_id: str, chat_id: int) -> datetime | None: ...


class UpdateLedger(Protocol):
    async def begin(self, update_id: int) -> LedgerStatus: ...
    async def complete(self, update_id: int) -> None: ...


@dataclass(frozen=True)
class Job:
    key: str
    kind: JobKind
    session: SessionId
    run_at: datetime
    payload: Mapping[str, JsonValue] = field(default_factory=lambda: MappingProxyType({}))


class Scheduler(Protocol):
    async def schedule(self, job: Job) -> None: ...
    async def cancel(self, key: str) -> None: ...
    async def due(self, now: datetime) -> list[Job]: ...
    async def complete(self, key: str) -> None: ...
    async def pending(self, session: SessionId) -> list[Job]: ...
