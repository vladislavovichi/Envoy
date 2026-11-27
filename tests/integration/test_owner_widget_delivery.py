import json
from datetime import timedelta
from uuid import UUID

import pytest

from envoy.app.memory import InMemoryOwnerViewStore, InMemorySessionRepository
from envoy.app.owner_view import TextOwnerView
from envoy.app.ports import OwnerTarget, SessionRecord
from envoy.app.renderer import TemplateRenderer
from envoy.domain.events import Delivered, OwnerResolved
from envoy.domain.ids import MandateId, OwnerId, SessionId
from envoy.domain.session import SessionState
from envoy.llm.translator import IntentWire
from envoy.ports.clock import FixedClock
from envoy.security.widget_projection import EscalationCard, IssueRow
from envoy.telegram.memory import FakeTransport
from envoy.widgets.owner_view import WidgetOwnerView, escalation_state
from tests.support.sessions import T0
from tests.support.widgets import (
    CONFIRMING,
    DASHBOARD,
    ESCALATION,
    counter_round,
    escalated,
    resolutions,
    state_of,
    states,
    widget_started,
)


class SimulatedCrash(BaseException):
    pass


async def test_dashboard_is_one_message_updated_with_growing_revisions() -> None:
    harness = await widget_started()
    await counter_round(harness, 1)
    await counter_round(harness, 2)
    dashboards = harness.widgets_of(DASHBOARD)
    widget_messages = len(dashboards)
    assert widget_messages == 1
    dashboard = dashboards[0]
    assert dashboard.thread_id == 1000
    assert dashboard.allow_queries is False
    assert dashboard.caption.startswith("Ход сделки: раунд 1 из 6")
    dashboard_revisions = [state["r"] for state in states(harness, dashboard.message.message_id)]
    assert dashboard_revisions == sorted(set(dashboard_revisions))
    assert len(dashboard_revisions) >= 3
    latest = state_of(harness, dashboard.message.message_id)
    assert latest["round"] == (await harness.view()).round
    assert latest["status"] == "counterpart"
    assert {row["name"] for row in latest["issues"]} == {"время", "место"}
    record = await harness.session()
    assert record.dashboard_widget.message_id == dashboard.message.message_id
    assert record.dashboard_widget.revision == latest["r"]


async def test_dashboard_skips_when_revision_did_not_grow() -> None:
    harness = await widget_started()
    edits = len(harness.transport.widget_edits)
    await harness.pipeline.session_flow.present(await harness.session())
    assert len(harness.transport.widget_edits) == edits


async def test_escalation_widget_has_text_caption_buttons_and_is_resolved_by_edit() -> None:
    harness = await escalated()
    cards = harness.widgets_of(ESCALATION)
    assert len(cards) == 1
    card = cards[0]
    assert card.allow_queries is True
    assert card.caption.startswith("Нужно решение")
    cause = (await harness.view()).pending.cause
    assert [button.data for button in card.reply_markup.buttons[0]] == [
        f"approve:{cause}",
        f"decline:{cause}",
    ]
    assert not [text for text in harness.to_owner() if text.startswith("Нужно решение")]
    state = state_of(harness, card.message.message_id)
    assert (state["cause"], state["outcome"], state["remaining"]) == (cause, "pending", 3600)
    assert state["expires_at"].endswith("Z")
    dashboard = harness.widgets_of(DASHBOARD)[0]
    assert state_of(harness, dashboard.message.message_id)["status"] == "owner"
    await harness.callback(f"approve:{cause}")
    assert state_of(harness, card.message.message_id)["outcome"] == "approved"
    assert state_of(harness, dashboard.message.message_id)["status"] == "agreed"
    assert (await harness.view()).state is SessionState.EXECUTING


async def test_send_widget_rejection_delivers_escalation_as_text() -> None:
    harness = await widget_started(CONFIRMING)
    harness.transport.widgets_available = False
    await harness.counterpart("ок", IntentWire(kind="accept"))
    cause = (await harness.view()).pending.cause
    cards = [record for record in harness.transport.sent if record.reply_markup is not None]
    assert cards[-1].message.text.startswith("Нужно решение")
    assert cards[-1].reply_markup.buttons[0][0].data == f"approve:{cause}"
    await harness.callback(f"approve:{cause}")
    assert (await harness.view()).state is SessionState.EXECUTING


def overflow_card(size: int) -> EscalationCard:
    base = EscalationCard(4, 42, T0 + timedelta(hours=1), (IssueRow("место", "", None),), "pending")
    padding = size - len(escalation_state(base, T0).encode("utf-8"))
    value = "я" * (padding // 2) + "x" * (padding % 2)
    card = EscalationCard(4, 42, base.expires_at, (IssueRow("место", value, None),), "pending")
    assert len(escalation_state(card, T0).encode("utf-8")) == size
    return card


@pytest.mark.parametrize(("size", "widget"), [(1024, True), (1025, False)])
async def test_escalation_state_limit_in_utf8_bytes(size, widget) -> None:
    transport = FakeTransport()
    store = InMemoryOwnerViewStore()
    renderer = TemplateRenderer("UTC")
    text = TextOwnerView(transport, renderer, store)
    clock = FixedClock(T0)
    view = WidgetOwnerView(transport, text, InMemorySessionRepository(), store, clock)
    card = overflow_card(size)
    caption = renderer.owner_escalation(card).text
    if len(caption) > 1024:
        pytest.skip("caption limit dominates for this size")
    session = SessionId(UUID(int=1))
    await view.escalation(OwnerTarget(OwnerId(UUID(int=2)), 1, 1, None), session, card)
    assert bool(transport.widgets) is widget
    if not widget:
        assert transport.sent[-1].reply_markup.buttons[0][0].data == "approve:42"
        assert (await store.get_escalation(session, 42)).version == 0


async def test_long_offer_uses_text_card_when_caption_does_not_fit() -> None:
    transport = FakeTransport()
    store = InMemoryOwnerViewStore()
    renderer = TemplateRenderer("UTC")
    view = WidgetOwnerView(
        transport,
        TextOwnerView(transport, renderer, store),
        InMemorySessionRepository(),
        store,
        FixedClock(T0),
    )
    card = EscalationCard(
        1, 42, T0 + timedelta(hours=1), (IssueRow("место", "<b>кафе</b>&" * 120, None),), "pending"
    )
    await view.escalation(
        OwnerTarget(OwnerId(UUID(int=2)), 1, 1, None), SessionId(UUID(int=1)), card
    )
    assert transport.widgets == {}
    assert "<b>кафе</b>&" in transport.sent[-1].message.text


async def test_crash_after_resolution_before_edit_recovers_cards_without_new_decision() -> None:
    harness = await escalated()
    card = harness.widgets_of(ESCALATION)[0]
    cause = (await harness.view()).pending.cause
    original = harness.transport.edit_widget

    async def crash(*args):
        raise SimulatedCrash

    harness.transport.edit_widget = crash
    with pytest.raises(SimulatedCrash):
        await harness.widget_query(f"a:{cause}", card.message.message_id)
    assert harness.widget_answers() == ["ok"]
    assert state_of(harness, card.message.message_id)["outcome"] == "pending"
    harness.transport.edit_widget = original
    record = await harness.session()
    await harness.pipeline.processor.resume(record)
    await harness.pipeline.processor.resume(record)
    events = await harness.events()
    assert len(resolutions(events, cause)) == 1
    assert state_of(harness, card.message.message_id)["outcome"] == "approved"
    dashboard = harness.widgets_of(DASHBOARD)[0]
    assert state_of(harness, dashboard.message.message_id)["status"] == "agreed"
    accepts = [text for text in harness.to_counterpart() if text.startswith("Договорились")]
    assert len(accepts) == 1


async def test_crash_after_resolution_before_continuation_resumes_action_once() -> None:
    harness = await escalated()
    cause = (await harness.view()).pending.cause
    flow = harness.pipeline.session_flow
    original = flow.proceed

    async def crash(*args):
        raise SimulatedCrash

    flow.proceed = crash
    with pytest.raises(SimulatedCrash):
        await harness.widget_query(
            f"a:{cause}", harness.widgets_of(ESCALATION)[0].message.message_id
        )
    flow.proceed = original
    events = await harness.events()
    assert isinstance(events[-1], OwnerResolved)
    await harness.pipeline.processor.resume(await harness.session())
    await harness.pipeline.processor.resume(await harness.session())
    events = await harness.events()
    assert len(resolutions(events, cause)) == 1
    assert len([event for event in events if isinstance(event, Delivered)]) == 2
    assert (await harness.view()).state is SessionState.EXECUTING


async def test_edit_applied_before_crash_counts_as_delivered_on_retry() -> None:
    harness = await widget_started()
    dashboard = harness.widgets_of(DASHBOARD)[0]
    original = harness.transport.edit_widget

    async def apply_then_crash(chat_id, message_id, state):
        await original(chat_id, message_id, state)
        raise SimulatedCrash

    harness.transport.edit_widget = apply_then_crash
    with pytest.raises(SimulatedCrash):
        await counter_round(harness, 1)
    harness.transport.edit_widget = original
    applied = len(harness.transport.widget_edits)
    record = await harness.session()
    await harness.pipeline.processor.resume(record)
    history = states(harness, dashboard.message.message_id)
    assert len(harness.transport.widget_edits) == applied
    assert [state["r"] for state in history] == sorted(set(state["r"] for state in history))
    stored = (await harness.session()).dashboard_widget
    assert stored.revision == history[-1]["r"]
    assert json.loads(harness.transport.widgets[dashboard.message.message_id].state) == history[-1]


def test_existing_record_defaults_to_no_dashboard() -> None:
    record = SessionRecord(
        SessionId(UUID(int=1)), OwnerId(UUID(int=1)), MandateId(UUID(int=1)), "c", 1, 1, None, T0
    )
    assert record.dashboard_widget is None
