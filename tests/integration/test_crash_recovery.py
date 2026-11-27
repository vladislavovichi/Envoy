import pytest

from envoy.domain.events import Decided, Delivered, Escalated
from envoy.domain.ids import owner_for_user
from envoy.domain.session import SessionState, fold
from envoy.domain.telegram import (
    BusinessConnection,
    BusinessConnectionUpdate,
    BusinessMessageUpdate,
)
from envoy.llm.fake import ScriptedLanguageModel
from envoy.llm.translator import IntentWire
from tests.integration.test_meeting_e2e import CONNECTION, COUNTERPART, OWNER, build
from tests.support.harness import message_is
from tests.support.mandates import meeting_mandate


class SimulatedCrash(BaseException):
    pass


class CrashingSender:
    def __init__(self, inner, predicate) -> None:
        self._inner = inner
        self._predicate = predicate

    async def send_message(self, chat_id, text, **kwargs):
        if self._predicate(text, kwargs):
            raise SimulatedCrash
        return await self._inner.send_message(chat_id, text, **kwargs)


def is_offer(text: str, kwargs: dict) -> bool:
    return bool(kwargs.get("business_connection_id")) and text.startswith("Предлагаю")


def business(update_id: int, user: int, text: str) -> BusinessMessageUpdate:
    return BusinessMessageUpdate(
        update_id, CONNECTION, COUNTERPART, user, message_id=update_id, text=text
    )


async def prepare(driver, mandate) -> None:
    store = driver.app.pipeline.stores.mandates
    draft = await store.save_draft(owner_for_user(OWNER), mandate, b"s" * 32)
    await driver.callback(f"mandate_confirm:{draft.id}")
    await driver.feed(
        BusinessConnectionUpdate(driver.next_id(), BusinessConnection(CONNECTION, OWNER, OWNER))
    )
    await driver.feed(business(900, COUNTERPART, "привет"))


def postgres_driver(pg_url, llm=None):
    return build(
        model=llm or ScriptedLanguageModel(), journal_store="postgres", database_url=pg_url
    )


async def session_events(driver):
    sessions = driver.app.pipeline.stores.sessions
    record = (await sessions.list_active())[0]
    return record, await driver.app.pipeline.stores.journal.events(record.id)


async def test_process_killed_after_decision_before_send_resumes_and_sends_once(
    pg_url, pg_session_factory
) -> None:
    first = postgres_driver(pg_url)
    try:
        await prepare(first, meeting_mandate())
        first.app.pipeline.steps.sender = CrashingSender(first.app.pipeline.steps.sender, is_offer)
        with pytest.raises(SimulatedCrash):
            await first.feed(business(901, OWNER, "/envoy"))
        _, events = await session_events(first)
        assert [e for e in events if isinstance(e, Decided)]
        assert not [e for e in events if isinstance(e, Delivered)]
    finally:
        await first.app.close()
    second = postgres_driver(pg_url)
    try:
        await second.feed(business(901, OWNER, "/envoy"))
        await second.app.runtime._recovery.resume()
        await second.feed(business(901, OWNER, "/envoy"))
        record, events = await session_events(second)
        delivered = [e for e in events if isinstance(e, Delivered)]
        assert len(delivered) == 1
        offers = [text for text in second.counterpart_texts() if text.startswith("Предлагаю")]
        assert len(offers) == 1
        assert fold(events).state is SessionState.NEGOTIATING
        assert fold(events).undelivered == ()
    finally:
        await second.app.close()


async def test_process_killed_with_pending_escalation_keeps_timeout_job(
    pg_url, pg_session_factory, mocker
) -> None:
    llm = ScriptedLanguageModel()
    llm.add(message_is("согласен"), IntentWire(kind="accept"))
    first = postgres_driver(pg_url, llm)
    try:
        await prepare(first, meeting_mandate(escalation={"always": ["accept_deal"]}))
        await first.feed(business(901, OWNER, "/envoy"))
        scheduler = first.app.pipeline.steps.scheduler
        original = scheduler.schedule

        async def crash_on_escalation(job):
            if job.kind == "escalation_timeout":
                raise SimulatedCrash
            await original(job)

        patched = mocker.patch.object(scheduler, "schedule", side_effect=crash_on_escalation)
        with pytest.raises(SimulatedCrash):
            await first.feed(business(902, COUNTERPART, "согласен"))
        mocker.stop(patched)
        record, events = await session_events(first)
        assert any(isinstance(e, Escalated) and e.reason == "confirm_action" for e in events)
        assert not [j for j in await scheduler.pending(record.id) if j.kind == "escalation_timeout"]
    finally:
        await first.app.close()
    second = postgres_driver(pg_url, llm)
    try:
        await second.app.runtime._recovery.resume()
        record, events = await session_events(second)
        jobs = await second.app.pipeline.stores.scheduler.pending(record.id)
        timeout = [job for job in jobs if job.kind == "escalation_timeout"]
        assert len(timeout) == 1
        assert timeout[0].run_at == fold(events).pending.expires_at
        assert fold(events).state is SessionState.PENDING_OWNER
    finally:
        await second.app.close()
