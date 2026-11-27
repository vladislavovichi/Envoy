from datetime import timedelta

import pytest

from envoy.domain.events import (
    ChecklistOpened,
    Closed,
    Decided,
    Delivered,
    Escalated,
    Outcome,
    OwnerResolved,
)
from envoy.domain.offer import Offer
from envoy.domain.session import SessionState
from envoy.llm.translator import IntentWire
from envoy.mandate.compiler import compile_mandate
from tests.support.harness import COUNTERPART, OWNER_USER, build_harness, propose
from tests.support.mandates import PLACES, SLOTS, meeting_mandate

CONFIRMING = meeting_mandate(escalation={"always": ["accept_deal"]})


async def started(mandate=None, *, greet: bool = True):
    harness = build_harness()
    await harness.connect()
    await harness.activate(mandate)
    if greet:
        await harness.counterpart("привет", IntentWire(kind="unclear"))
    await harness.owner_in_chat("/envoy")
    return harness


def decided(events) -> list[Decided]:
    return [event for event in events if isinstance(event, Decided)]


async def test_open_discloses_then_offers_within_mandate() -> None:
    harness = await started()
    texts = harness.to_counterpart()
    assert "ИИ-агент" in texts[0]
    assert texts[1].startswith("Предлагаю")
    view = await harness.view()
    assert view.state is SessionState.NEGOTIATING
    assert view.disclosed
    assert view.last_outgoing == Offer(values={"time": SLOTS[0], "place": "office"})
    assert any("https://t.me/envoy_bot/1000" in text for text in harness.to_owner())


async def test_redelivered_update_produces_single_reply() -> None:
    harness = await started()
    update = await harness.counterpart("давай в кафе", propose(SLOTS[0].isoformat(), "cafe"))
    before = len(harness.to_counterpart())
    await harness.feed(update)
    await harness.feed(update)
    assert len(harness.to_counterpart()) == before
    assert len(harness.llm.requests) == 1


async def test_edited_message_is_ignored() -> None:
    harness = await started()
    before = len(harness.to_counterpart())
    await harness.counterpart("правка", IntentWire(kind="reject"), edited=True)
    assert len(harness.to_counterpart()) == before
    assert harness.llm.requests == []


async def test_reply_parked_when_window_closed_and_sent_once_after_reopen() -> None:
    harness = await started(greet=False)
    view = await harness.view()
    assert not view.disclosed
    assert harness.to_counterpart() == []
    assert any("24 часов" in text for text in harness.to_owner())
    assert any(isinstance(event, Escalated) for event in await harness.events())
    await harness.counterpart("я тут", propose(SLOTS[1].isoformat(), "cafe"))
    texts = harness.to_counterpart()
    assert "ИИ-агент" in texts[0]
    assert len(texts) == 2
    view = await harness.view()
    assert view.undelivered == ()


async def test_offer_parked_after_24h_and_flushed_once() -> None:
    harness = await started()
    harness.clock.advance(timedelta(hours=25))
    await harness.pipeline.session_flow.flush_undelivered(await harness.session())
    record = await harness.session()
    ctx = await harness.pipeline.steps.load(record)
    from envoy.security.authorizer import Action

    await harness.pipeline.session_flow._act(
        ctx, 555, Action("send_offer", Offer(values={"time": SLOTS[1], "place": "office"}))
    )
    view = await harness.view()
    assert [decision.cause for decision in view.undelivered] == [555]
    before = len(harness.to_counterpart())
    await harness.counterpart("ау", IntentWire(kind="unclear", missing=["time"]))
    assert len(harness.to_counterpart()) == before + 2
    await harness.pipeline.session_flow.flush_undelivered(record)
    assert len(harness.to_counterpart()) == before + 2


async def test_counter_offers_never_leave_mandate() -> None:
    harness = await started()
    compiled = compile_mandate(meeting_mandate())
    for index, place in enumerate(["station", "park", "cafe", "station"]):
        await harness.counterpart(f"вариант {index}", propose(SLOTS[7].isoformat(), place))
    for decision in decided(await harness.events()):
        if decision.offer is not None and decision.verdict == "allow":
            assert compiled.constraints.is_feasible(decision.offer)
            assert compiled.utility.value(decision.offer) >= compiled.reservation


async def test_counterpart_accept_reaches_checklist() -> None:
    harness = await started()
    await harness.counterpart("ок, согласен", IntentWire(kind="accept"))
    view = await harness.view()
    assert view.state is SessionState.EXECUTING
    assert harness.to_counterpart()[-1].startswith("Договорились")
    assert harness.transport.checklists[0].items[0].startswith("Владелец")


async def test_approve_executes_accept_once_on_double_click() -> None:
    harness = await started(CONFIRMING)
    await harness.counterpart("ок", IntentWire(kind="accept"))
    view = await harness.view()
    assert view.state is SessionState.PENDING_OWNER
    cause = view.pending.cause
    card = next(record for record in harness.transport.sent if record.reply_markup is not None)
    assert card.message.text.startswith("Нужно решение")
    assert card.thread_id == 1000
    await harness.callback(f"approve:{cause}")
    await harness.callback(f"approve:{cause}")
    accepts = [text for text in harness.to_counterpart() if text.startswith("Договорились")]
    assert len(accepts) == 1
    assert len(harness.transport.checklists) == 1
    assert harness.transport.callback_answers[-1][1] == "Решение уже не требуется"
    assert (await harness.view()).state is SessionState.EXECUTING


async def test_late_callback_after_timeout_is_rejected_without_action() -> None:
    harness = await started(CONFIRMING)
    await harness.counterpart("ок", IntentWire(kind="accept"))
    cause = (await harness.view()).pending.cause
    harness.clock.advance(timedelta(hours=2))
    await harness.run_due_jobs()
    view = await harness.view()
    assert view.state is SessionState.CLOSED
    assert view.outcome is Outcome.expired
    await harness.callback(f"approve:{cause}")
    assert not any(text.startswith("Договорились") for text in harness.to_counterpart())
    assert harness.transport.checklists == []


async def test_callback_from_non_owner_ignored() -> None:
    harness = await started(CONFIRMING)
    await harness.counterpart("ок", IntentWire(kind="accept"))
    cause = (await harness.view()).pending.cause
    await harness.callback(f"approve:{cause}", user_id=COUNTERPART)
    assert (await harness.view()).state is SessionState.PENDING_OWNER
    assert len(harness.transport.callback_answers) == 1


async def test_decline_rejects_and_closes() -> None:
    harness = await started(CONFIRMING)
    await harness.counterpart("ок", IntentWire(kind="accept"))
    cause = (await harness.view()).pending.cause
    await harness.callback(f"decline:{cause}")
    events = await harness.events()
    assert OwnerResolved(cause=cause, resolution="declined") in events
    view = await harness.view()
    assert view.state is SessionState.CLOSED
    assert view.outcome is Outcome.rejected
    assert "договориться не получится" in harness.to_counterpart()[-1]


async def test_escalation_timeout_job_denies_and_closes_expired() -> None:
    harness = await started(CONFIRMING)
    await harness.counterpart("ок", IntentWire(kind="accept"))
    harness.clock.advance(timedelta(minutes=59))
    await harness.run_due_jobs()
    assert (await harness.view()).state is SessionState.PENDING_OWNER
    harness.clock.advance(timedelta(minutes=2))
    await harness.run_due_jobs()
    events = await harness.events()
    assert any(
        isinstance(event, OwnerResolved) and event.resolution == "timeout" for event in events
    )
    assert isinstance(events[-1], Closed)
    assert events[-1].outcome is Outcome.expired
    assert await harness.pipeline.stores.scheduler.pending((await harness.session()).id) == []


async def test_session_deadline_closes_negotiation() -> None:
    harness = await started()
    harness.clock.advance(timedelta(days=2, minutes=1))
    await harness.run_due_jobs()
    view = await harness.view()
    assert (view.state, view.outcome) == (SessionState.CLOSED, Outcome.expired)


async def test_counterpart_reject_closes_rejected() -> None:
    harness = await started()
    await harness.counterpart("нет, не надо", IntentWire(kind="reject"))
    view = await harness.view()
    assert (view.state, view.outcome) == (SessionState.CLOSED, Outcome.rejected)
    assert await harness.pipeline.stores.sessions.list_active() == []


async def test_messages_after_close_are_ignored() -> None:
    harness = await started()
    await harness.counterpart("нет", IntentWire(kind="reject"))
    before = len(harness.to_counterpart())
    await harness.counterpart("а может да", IntentWire(kind="accept"))
    assert len(harness.to_counterpart()) == before


@pytest.mark.parametrize("place", PLACES[:2])
async def test_journal_entries_verify(place: str) -> None:
    from envoy.journal.verifier import JournalVerifier
    from tests.support.journal import AGENT

    harness = await started()
    await harness.counterpart("предложение", propose(SLOTS[0].isoformat(), place))
    record = await harness.session()
    entries = await harness.journal_store.read(record.id)
    JournalVerifier().verify(entries, {AGENT: harness.signing_key.public()})
    assert len([event for event in await harness.events() if isinstance(event, Delivered)]) >= 2
    assert OWNER_USER == record.owner_chat_id
    assert any(isinstance(event, ChecklistOpened) for event in await harness.events()) is (
        place == "office"
    )
