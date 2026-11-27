from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from uuid import UUID

import pytest

from envoy.app.memory import InMemoryOwnerViewStore, InMemorySessionRepository
from envoy.app.ports import OwnerTarget, SessionRecord, SummaryWidgetRef, WidgetRef
from envoy.domain.ids import MandateId, OwnerId, SessionId
from envoy.persistence.owner_views import PgOwnerViewStore
from envoy.persistence.state_stores import PgSessionRepository
from envoy.security.widget_projection import SummaryRow

T0 = datetime(2026, 12, 1, 12, tzinfo=UTC)
OWNER = OwnerId(UUID(int=1))
OTHER = OwnerId(UUID(int=2))
SESSION = SessionId(UUID(int=10))
DAY = date(2026, 12, 1)


@pytest.fixture(params=["memory", "postgres"])
def backend(request):
    return request.param


@pytest.fixture
def factory(backend, request):
    return None if backend == "memory" else request.getfixturevalue("pg_session_factory")


@pytest.fixture
def store(backend, factory):
    return InMemoryOwnerViewStore() if backend == "memory" else PgOwnerViewStore(factory)


@pytest.fixture
def sessions(backend, factory):
    return InMemorySessionRepository() if backend == "memory" else PgSessionRepository(factory)


def record(identity: int = 10) -> SessionRecord:
    return SessionRecord(
        id=SessionId(UUID(int=identity)),
        owner_id=OWNER,
        mandate_id=MandateId(UUID(int=3)),
        connection_id="conn",
        counterpart_chat_id=2,
        owner_chat_id=1,
        topic_id=None,
        deadline=T0 + timedelta(days=1),
    )


async def test_existing_session_reads_without_dashboard_and_stores_ref(sessions) -> None:
    await sessions.create(record())
    assert (await sessions.get(SESSION)).dashboard_widget is None
    ref = WidgetRef(1, 50, 1, 7)
    await sessions.set_dashboard_widget(SESSION, ref)
    assert (await sessions.get(SESSION)).dashboard_widget == ref
    assert (await sessions.list_active())[0].dashboard_widget == ref
    await sessions.set_dashboard_widget(SESSION, WidgetRef(1, 50, 1, 9))
    assert (await sessions.get(SESSION)).dashboard_widget.revision == 9


async def test_escalation_binding_is_current_and_unique_per_message(store) -> None:
    assert await store.get_escalation(SESSION, 42) is None
    assert await store.find_escalation(1, 50) is None
    await store.put_escalation(SESSION, 42, WidgetRef(1, 50, 1, 3))
    assert await store.find_escalation(1, 50) == (SESSION, 42)
    assert await store.find_escalation(2, 50) is None
    await store.put_escalation(SESSION, 42, WidgetRef(1, 50, 1, 5))
    assert (await store.get_escalation(SESSION, 42)).revision == 5
    await store.put_escalation(SESSION, 42, WidgetRef(1, 51, 1, 6))
    assert await store.find_escalation(1, 50) is None
    assert await store.find_escalation(1, 51) == (SESSION, 42)
    with pytest.raises(Exception):  # noqa: B017, PT011
        await store.put_escalation(SESSION, 43, WidgetRef(1, 51, 1, 1))
    assert await store.find_escalation(1, 51) == (SESSION, 42)


async def test_summary_refs_delivery_marks_and_topics(store) -> None:
    target = OwnerTarget(OWNER, 1, 1, 77)
    rows = (
        SummaryRow("Чат 2", "owner", "https://t.me/envoy_bot/5"),
        SummaryRow("Чат 3", "agreed", None),
    )
    summary = SummaryWidgetRef(target, DAY, WidgetRef(1, 90, 1, 1), rows)
    assert await store.get_summary(OWNER, DAY) is None
    await store.put_summary(summary)
    assert await store.get_summary(OWNER, DAY) == summary
    assert await store.find_summary(1, 90) == summary
    assert await store.find_summary(1, 91) is None
    assert await store.get_summary(OTHER, DAY) is None
    assert not await store.summary_delivered(OWNER, DAY)
    await store.mark_summary_delivered(OWNER, DAY)
    await store.mark_summary_delivered(OWNER, DAY)
    assert await store.summary_delivered(OWNER, DAY)
    assert not await store.summary_delivered(OWNER, DAY + timedelta(days=1))
    assert await store.summary_topic(OWNER) is None
    await store.set_summary_topic(OWNER, 77)
    await store.set_summary_topic(OWNER, 78)
    assert await store.summary_topic(OWNER) == 78


async def test_mandate_card_binding_round_trips(backend, factory) -> None:
    from envoy.app.mandate_widget import InMemoryMandateCardStore, MandateCard
    from envoy.persistence.owner_views import PgMandateCardStore

    store = InMemoryMandateCardStore() if backend == "memory" else PgMandateCardStore(factory)
    card = MandateCard(
        OWNER,
        1,
        1,
        70,
        3,
        MandateId(UUID(int=4)),
        (T0, T0 + timedelta(hours=1)),
        (True, False),
        ("place", "time"),
    )
    assert await store.find(1, 70) is None
    await store.put(card)
    assert await store.find(1, 70) == card
    await store.put(replace(card, revision=4))
    assert (await store.find(1, 70)).revision == 4
    assert await store.find(2, 70) is None
