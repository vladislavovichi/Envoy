import asyncio
from datetime import timedelta

import pytest

from envoy.domain.events import Decided, OwnerResolved
from envoy.domain.session import SessionState
from envoy.domain.telegram import WidgetQueryUpdate
from envoy.security.authorizer import Deny
from tests.support.harness import OWNER_USER
from tests.support.widgets import ESCALATION, escalated, resolutions

OTHER_OWNER = 3


async def pending(harness) -> tuple[int, int]:
    view = await harness.view()
    card = harness.widgets_of(ESCALATION)[0]
    return view.pending.cause, card.message.message_id


def accepts(harness) -> list[str]:
    return [text for text in harness.to_counterpart() if text.startswith("Договорились")]


async def test_two_update_ids_with_same_request_record_one_decision() -> None:
    harness = await escalated()
    cause, message_id = await pending(harness)
    await harness.widget_query(f"a:{cause}", message_id)
    await harness.widget_query(f"a:{cause}", message_id)
    assert len(resolutions(await harness.events(), cause)) == 1
    assert harness.widget_answers() == ["ok", "stale"]
    assert len(accepts(harness)) == 1


async def test_same_update_redelivered_is_processed_once() -> None:
    harness = await escalated()
    cause, message_id = await pending(harness)
    update = await harness.widget_query(f"a:{cause}", message_id)
    await harness.feed(update)
    assert harness.widget_answers() == ["ok"]
    assert len(resolutions(await harness.events(), cause)) == 1


async def test_callback_then_widget_and_widget_then_callback() -> None:
    harness = await escalated()
    cause, message_id = await pending(harness)
    await harness.callback(f"approve:{cause}")
    await harness.widget_query(f"d:{cause}", message_id)
    assert harness.widget_answers() == ["stale"]
    assert len(resolutions(await harness.events(), cause)) == 1
    assert (await harness.view()).state is SessionState.EXECUTING

    harness = await escalated()
    cause, message_id = await pending(harness)
    await harness.widget_query(f"d:{cause}", message_id)
    await harness.callback(f"approve:{cause}")
    assert harness.transport.callback_answers[-1][1] == "Решение уже не требуется"
    events = await harness.events()
    assert [event.resolution for event in resolutions(events, cause)] == ["declined"]
    assert accepts(harness) == []


async def test_concurrent_approve_and_decline_from_text_and_widget() -> None:
    harness = await escalated()
    cause, message_id = await pending(harness)
    await asyncio.gather(
        harness.widget_query(f"a:{cause}", message_id),
        harness.callback(f"decline:{cause}"),
        harness.widget_query(f"d:{cause}", message_id),
    )
    events = await harness.events()
    assert len(resolutions(events, cause)) == 1
    after = events[events.index(resolutions(events, cause)[0]) + 1 :]
    allowed = [event for event in after if isinstance(event, Decided) and event.verdict == "allow"]
    assert len(allowed) == 1
    assert len(accepts(harness)) <= 1


async def test_deadline_boundary_is_expired_and_timeout_wins() -> None:
    harness = await escalated()
    cause, message_id = await pending(harness)
    expires = (await harness.view()).pending.expires_at
    harness.clock.advance(expires - harness.clock.now())
    await harness.widget_query(f"a:{cause}", message_id)
    assert harness.widget_answers() == ["expired"]
    assert resolutions(await harness.events(), cause) == []
    await harness.run_due_jobs()
    assert [event.resolution for event in resolutions(await harness.events(), cause)] == ["timeout"]
    assert accepts(harness) == []


async def test_callback_at_deadline_is_rejected() -> None:
    harness = await escalated()
    cause, _ = await pending(harness)
    expires = (await harness.view()).pending.expires_at
    harness.clock.advance(expires - harness.clock.now())
    await harness.callback(f"approve:{cause}")
    assert harness.transport.callback_answers[-1][1] == "Время на решение истекло"
    assert resolutions(await harness.events(), cause) == []


@pytest.mark.parametrize(
    "case",
    [
        "foreign_owner",
        "foreign_owner_in_owner_chat",
        "stranger",
        "wrong_chat",
        "stale_message",
        "unsupported_type",
        "leading_zero",
        "negative",
        "other_cause",
        "garbage",
        "no_message",
    ],
)
async def test_rejected_queries_get_answer_and_no_action(case) -> None:
    harness = await escalated(owner_ids=frozenset({OWNER_USER, OTHER_OWNER}))
    cause, message_id = await pending(harness)
    before = len(await harness.events())
    data, mid, user, chat = f"a:{cause}", message_id, OWNER_USER, None
    match case:
        case "foreign_owner":
            user = OTHER_OWNER
        case "foreign_owner_in_owner_chat":
            user, chat = OTHER_OWNER, OWNER_USER
        case "stranger":
            user = 99
        case "wrong_chat":
            chat = 555
        case "stale_message":
            mid = message_id + 100
        case "unsupported_type":
            data = None
        case "leading_zero":
            data = f"a:0{cause}"
        case "negative":
            data = f"a:-{cause}"
        case "other_cause":
            data = f"a:{cause + 1}"
        case "garbage":
            data = "approve"
        case "no_message":
            mid = None
    update = await harness.widget_query(data, mid, user_id=user, chat_id=chat)
    assert len(await harness.events()) == before
    assert harness.transport.widget_answers == [(update.query_id, "no")]


async def test_text_card_message_is_not_a_widget_binding() -> None:
    harness = await escalated()
    harness.transport.widgets_available = False
    cause, _ = await pending(harness)
    await harness.widget_query(f"a:{cause}", 10_000)
    assert harness.widget_answers() == ["no"]


class SlowCounterpart:
    def __init__(self, transport) -> None:
        self.transport = transport
        self.release = asyncio.Event()
        self.original = transport.send_message

    async def send_message(self, chat_id, text, **kwargs):
        if kwargs.get("business_connection_id"):
            await self.release.wait()
        return await self.original(chat_id, text, **kwargs)


async def test_delayed_outbound_does_not_block_widget_answer() -> None:
    harness = await escalated()
    cause, message_id = await pending(harness)
    slow = SlowCounterpart(harness.transport)
    harness.pipeline.steps.sender.send_message = slow.send_message
    task = asyncio.create_task(harness.widget_query(f"a:{cause}", message_id))
    for _ in range(100):
        if harness.widget_answers():
            break
        await asyncio.sleep(0)
    assert harness.widget_answers() == ["ok"]
    assert not task.done()
    assert accepts(harness) == []
    slow.release.set()
    await task
    assert len(accepts(harness)) == 1


async def test_lock_wait_past_budget_answers_busy_without_decision(monkeypatch) -> None:
    import envoy.app.widget_queries as module

    harness = await escalated()
    cause, message_id = await pending(harness)
    monkeypatch.setattr(module, "DECISION_BUDGET", timedelta(milliseconds=50))
    record = await harness.session()
    lock = harness.pipeline.processor._locks.for_key(
        (record.connection_id, record.counterpart_chat_id)
    )
    async with lock:
        await harness.widget_query(f"a:{cause}", message_id)
    assert harness.widget_answers() == ["wait"]
    assert resolutions(await harness.events(), cause) == []


async def test_late_query_beyond_budget_is_not_recorded() -> None:
    harness = await escalated()
    cause, message_id = await pending(harness)
    identity = harness.update_id()
    update = WidgetQueryUpdate(
        identity,
        f"wq{identity}",
        OWNER_USER,
        OWNER_USER,
        message_id,
        f"a:{cause}",
        harness.clock.now() - timedelta(seconds=11),
    )
    await harness.feed(update)
    assert harness.widget_answers() == ["wait"]
    assert resolutions(await harness.events(), cause) == []


async def test_denied_continuation_does_not_reach_counterpart() -> None:
    harness = await escalated()
    cause, message_id = await pending(harness)
    authorizer = harness.pipeline.steps.authorizer
    original = authorizer.authorize

    def deny_approved(action, ctx):
        if ctx.approved:
            return Deny("deal_limit")
        return original(action, ctx)

    authorizer.authorize = deny_approved
    await harness.widget_query(f"a:{cause}", message_id)
    events = await harness.events()
    assert len(resolutions(events, cause)) == 1
    assert any(isinstance(event, Decided) and event.verdict == "deny" for event in events)
    assert accepts(harness) == []
    assert harness.widget_answers() == ["ok"]


async def test_query_expired_answer_is_logged_and_decision_kept() -> None:
    from envoy.telegram.ports import QueryExpired

    harness = await escalated()
    cause, message_id = await pending(harness)

    async def expired(*args, **kwargs):
        raise QueryExpired("too old")

    harness.transport.answer_widget = expired
    await harness.widget_query(f"a:{cause}", message_id)
    assert [event.resolution for event in resolutions(await harness.events(), cause)] == [
        "approved"
    ]
    assert isinstance((await harness.events())[-1], OwnerResolved) is False
