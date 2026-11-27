import asyncio
from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest

from envoy.app.memory import (
    InMemoryBusinessConnectionRepository,
    InMemoryScheduler,
    InMemorySessionRepository,
    InMemoryUpdateLedger,
)
from envoy.app.ports import Job, SessionRecord
from envoy.crypto.aead import ContentCipher
from envoy.domain.ids import MandateId, OwnerId, SessionId
from envoy.domain.telegram import BusinessConnection
from envoy.llm.budget import InMemoryUsageLedger
from envoy.mandate.ports import InMemoryMandateStore, MandateNotFound, MandateStateError
from envoy.persistence.state_stores import (
    PgBusinessConnectionRepository,
    PgMandateStore,
    PgScheduler,
    PgSessionRepository,
    PgUpdateLedger,
    PgUsageLedger,
)
from envoy.ports.ids import SequentialIdGenerator, UuidV7Generator
from envoy.ports.rng import SystemRng
from tests.support.mandates import meeting_mandate

T0 = datetime(2026, 12, 1, 12, tzinfo=UTC)
OWNER = OwnerId(UUID(int=1))
OTHER_OWNER = OwnerId(UUID(int=2))


@pytest.fixture(params=["memory", "postgres"])
def backend(request):
    return request.param


@pytest.fixture
def factory(backend, request):
    if backend == "memory":
        return None
    return request.getfixturevalue("pg_session_factory")


@pytest.fixture
def mandates(backend, factory):
    if backend == "memory":
        return InMemoryMandateStore(SequentialIdGenerator())
    cipher = ContentCipher(SystemRng())
    return PgMandateStore(factory, UuidV7Generator(), cipher, cipher.generate_key())


@pytest.fixture
def sessions(backend, factory):
    return InMemorySessionRepository() if backend == "memory" else PgSessionRepository(factory)


@pytest.fixture
def connections(backend, factory):
    if backend == "memory":
        return InMemoryBusinessConnectionRepository()
    return PgBusinessConnectionRepository(factory)


@pytest.fixture
def ledger(backend, factory):
    return InMemoryUpdateLedger() if backend == "memory" else PgUpdateLedger(factory)


@pytest.fixture
def scheduler(backend, factory):
    return InMemoryScheduler() if backend == "memory" else PgScheduler(factory)


@pytest.fixture
def usage(backend, factory):
    return InMemoryUsageLedger() if backend == "memory" else PgUsageLedger(factory)


def session_record(number: int, **overrides: object) -> SessionRecord:
    values: dict = {
        "id": SessionId(UUID(int=100 + number)),
        "owner_id": OWNER,
        "mandate_id": MandateId(UUID(int=5)),
        "connection_id": "conn",
        "counterpart_chat_id": 10 + number,
        "owner_chat_id": 1,
        "topic_id": None,
        "deadline": T0 + timedelta(days=1),
    }
    values.update(overrides)
    return SessionRecord(**values)


async def test_activate_supersedes_previous_version(mandates) -> None:
    first = await mandates.save_draft(OWNER, meeting_mandate(), b"salt1")
    assert (first.version, first.status, first.mandate.version) == (1, "draft", 1)
    await mandates.activate(first.id, b"sig1")
    second = await mandates.save_draft(OWNER, meeting_mandate(), b"salt2")
    assert second.version == 2
    activated = await mandates.activate(second.id, b"sig2")
    assert (activated.status, activated.signature) == ("active", b"sig2")
    assert (await mandates.get(first.id)).status == "superseded"
    assert (await mandates.get(second.id)).mandate == second.mandate
    with pytest.raises(MandateStateError):
        await mandates.activate(first.id, b"again")


async def test_active_returns_latest_activated_only(mandates) -> None:
    assert await mandates.active(OWNER) is None
    draft = await mandates.save_draft(OWNER, meeting_mandate(), b"s")
    assert await mandates.active(OWNER) is None
    await mandates.activate(draft.id, b"sig")
    pending = await mandates.save_draft(OWNER, meeting_mandate(), b"s")
    active = await mandates.active(OWNER)
    assert active is not None
    assert active.id == draft.id
    assert await mandates.active(OTHER_OWNER) is None
    discarded = await mandates.discard(pending.id)
    assert discarded.status == "discarded"
    with pytest.raises(MandateStateError):
        await mandates.discard(pending.id)


async def test_unknown_mandate_raises(mandates) -> None:
    with pytest.raises(MandateNotFound):
        await mandates.get(MandateId(UUID(int=999)))
    with pytest.raises(MandateNotFound):
        await mandates.activate(MandateId(UUID(int=999)), b"")


async def test_find_active_ignores_closed(sessions) -> None:
    record = session_record(1)
    await sessions.create(record)
    assert await sessions.get(record.id) == record
    assert await sessions.find_active("conn", 11) == record
    assert await sessions.find_active("conn", 12) is None
    assert await sessions.list_active() == [record]
    await sessions.mark_closed(record.id, T0)
    await sessions.mark_closed(record.id, T0 + timedelta(hours=1))
    assert await sessions.find_active("conn", 11) is None
    assert await sessions.list_active() == []
    assert (await sessions.get(record.id)).closed_at == T0
    assert await sessions.get(SessionId(UUID(int=1))) is None


async def test_count_agreed_since_boundary(sessions) -> None:
    for number in range(3):
        await sessions.create(session_record(number))
    await sessions.mark_agreed(SessionId(UUID(int=100)), T0)
    await sessions.mark_agreed(SessionId(UUID(int=101)), T0 + timedelta(hours=1))
    await sessions.mark_agreed(SessionId(UUID(int=101)), T0 + timedelta(hours=5))
    assert await sessions.count_agreed_since(OWNER, T0) == 2
    assert await sessions.count_agreed_since(OWNER, T0 + timedelta(microseconds=1)) == 1
    assert await sessions.count_agreed_since(OTHER_OWNER, T0) == 0


async def test_connection_upsert_and_get(connections) -> None:
    assert await connections.get("conn") is None
    await connections.upsert(BusinessConnection("conn", 1, 1, {"can_reply": True}, True))
    await connections.upsert(BusinessConnection("conn", 1, 1, {"can_reply": False}, False))
    stored = await connections.get("conn")
    assert stored is not None
    assert (dict(stored.rights), stored.enabled) == ({"can_reply": False}, False)


async def test_touch_chat_keeps_latest(connections) -> None:
    assert await connections.last_incoming_at("conn", 7) is None
    await connections.touch_chat("conn", 7, T0 + timedelta(hours=2))
    await connections.touch_chat("conn", 7, T0)
    assert await connections.last_incoming_at("conn", 7) == T0 + timedelta(hours=2)
    await connections.touch_chat("conn", 7, T0 + timedelta(hours=3))
    assert await connections.last_incoming_at("conn", 7) == T0 + timedelta(hours=3)


async def test_ledger_in_progress_then_done(ledger) -> None:
    assert await ledger.begin(5) == "new"
    assert await ledger.begin(5) == "in_progress"
    await ledger.complete(5)
    assert await ledger.begin(5) == "done"


async def test_ledger_done_is_stable_on_repeat_begin(ledger) -> None:
    await ledger.complete(6)
    assert [await ledger.begin(6) for _ in range(3)] == ["done"] * 3


async def test_ledger_concurrent_begin_has_single_new(ledger) -> None:
    results = await asyncio.gather(*(ledger.begin(7) for _ in range(5)))
    assert results.count("new") == 1


def job(key: str, minutes: int, session: int = 1) -> Job:
    return Job(
        key,
        "escalation_timeout",
        SessionId(UUID(int=session)),
        T0 + timedelta(minutes=minutes),
        {"cause": minutes},
    )


async def test_schedule_is_idempotent_by_key(scheduler) -> None:
    await scheduler.schedule(job("a", 1))
    await scheduler.schedule(job("a", 50))
    due = await scheduler.due(T0 + timedelta(hours=2))
    assert [(item.key, item.run_at) for item in due] == [("a", T0 + timedelta(minutes=1))]
    assert dict(due[0].payload) == {"cause": 1}
    await scheduler.complete("a")
    await scheduler.schedule(job("a", 1))
    assert await scheduler.due(T0 + timedelta(hours=2)) == []


async def test_due_returns_only_ripe_jobs_in_order(scheduler) -> None:
    for key, minutes in [("late", 30), ("b", 10), ("a", 10), ("future", 90)]:
        await scheduler.schedule(job(key, minutes))
    due = await scheduler.due(T0 + timedelta(minutes=30))
    assert [item.key for item in due] == ["a", "b", "late"]


async def test_cancel_removes_job(scheduler) -> None:
    await scheduler.schedule(job("a", 1))
    await scheduler.schedule(job("b", 2, session=2))
    await scheduler.cancel("a")
    await scheduler.cancel("missing")
    assert [item.key for item in await scheduler.due(T0 + timedelta(hours=1))] == ["b"]
    assert await scheduler.pending(SessionId(UUID(int=1))) == []
    assert [item.key for item in await scheduler.pending(SessionId(UUID(int=2)))] == ["b"]


async def test_usage_add_accumulates_per_day(usage) -> None:
    day = T0.date()
    assert await usage.add(day, 10) == 10
    assert await usage.add(day, 5) == 15
    assert await usage.add(day, 0) == 15
    assert await usage.add(day + timedelta(days=1), 3) == 3
