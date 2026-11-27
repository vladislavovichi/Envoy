from datetime import timedelta

from envoy.domain.events import CommitmentDone, Escalated, Outcome
from envoy.domain.session import SessionState
from envoy.llm.translator import IntentWire
from tests.support.harness import COUNTERPART, OWNER_USER, build_harness


async def agreed(*, keep_window: bool = True):
    harness = build_harness()
    await harness.connect()
    await harness.activate()
    await harness.counterpart("привет", IntentWire(kind="unclear"))
    await harness.owner_in_chat("/envoy")
    await harness.counterpart("согласен", IntentWire(kind="accept"))
    return harness


async def test_checklist_sent_after_agreement() -> None:
    harness = await agreed()
    checklist = harness.transport.checklists[0]
    assert checklist.business_connection_id == "conn"
    assert checklist.message.chat_id == COUNTERPART
    assert len(checklist.items) == 2
    view = await harness.view()
    assert view.state is SessionState.EXECUTING
    assert any(text.startswith("Сделка согласована") for text in harness.to_owner())


async def test_owner_and_counterpart_marks_map_to_parties() -> None:
    harness = await agreed()
    await harness.done(OWNER_USER, 1)
    await harness.done(COUNTERPART, 1)
    view = await harness.view()
    assert view.commitments_done == {("owner", 0), ("counterpart", 0)}
    assert view.state is SessionState.EXECUTING


async def test_duplicate_done_update_is_idempotent() -> None:
    harness = await agreed()
    update = await harness.done(OWNER_USER, 1)
    await harness.feed(update)
    await harness.done(OWNER_USER, 1, 7)
    done = [event for event in await harness.events() if isinstance(event, CommitmentDone)]
    assert done == [CommitmentDone(party="owner", index=0)]


async def test_closed_fulfilled_after_all_marked() -> None:
    harness = await agreed()
    await harness.done(OWNER_USER, 1)
    await harness.done(COUNTERPART, 2)
    view = await harness.view()
    assert (view.state, view.outcome) == (SessionState.CLOSED, Outcome.fulfilled)
    assert await harness.pipeline.stores.scheduler.pending((await harness.session()).id) == []
    assert "Встреча состоялась" in harness.to_owner()[-1]


async def test_deadline_job_closes_expired() -> None:
    harness = await agreed()
    await harness.done(OWNER_USER, 1)
    harness.clock.advance(timedelta(days=3))
    await harness.run_due_jobs()
    view = await harness.view()
    assert (view.state, view.outcome) == (SessionState.CLOSED, Outcome.expired)


async def test_negotiation_deadline_does_not_close_execution() -> None:
    harness = await agreed()
    record = await harness.session()
    jobs = await harness.pipeline.stores.scheduler.pending(record.id)
    negotiation = next(job for job in jobs if job.key.endswith(":deadline"))
    await harness.pipeline.processor.run_job(negotiation)
    assert (await harness.view()).state is SessionState.EXECUTING


async def test_reminder_scheduled_before_meeting() -> None:
    harness = await agreed()
    record = await harness.session()
    jobs = {job.kind: job for job in await harness.pipeline.stores.scheduler.pending(record.id)}
    view = await harness.view()
    meeting = view.agreed.values["time"]
    assert jobs["commitment_reminder"].run_at == max(
        meeting - timedelta(hours=1), harness.clock.now()
    )
    await harness.pipeline.processor.run_job(jobs["commitment_reminder"])
    assert harness.to_owner()[-1].startswith("Напоминание")
    assert harness.to_counterpart()[-1].startswith("Напоминаю")


async def test_checklist_parked_when_window_closed() -> None:
    harness = build_harness()
    await harness.connect()
    await harness.activate()
    await harness.counterpart("привет", IntentWire(kind="unclear"))
    await harness.owner_in_chat("/envoy")
    harness.transport.checklists.clear()
    original = harness.pipeline.execution._steps.window_open
    calls = {"count": 0}

    async def window_open(record):
        calls["count"] += 1
        return calls["count"] < 3 and await original(record)

    harness.pipeline.execution._steps.window_open = window_open
    await harness.counterpart("согласен", IntentWire(kind="accept"))
    view = await harness.view()
    assert view.state is SessionState.AGREED
    assert harness.transport.checklists == []
    assert isinstance((await harness.events())[-1], Escalated)
    harness.pipeline.execution._steps.window_open = original
    await harness.counterpart("ну что?", IntentWire(kind="unclear"))
    assert len(harness.transport.checklists) == 1
    assert (await harness.view()).state is SessionState.EXECUTING
