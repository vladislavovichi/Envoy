import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from aiohttp import web

from envoy.app.bootstrap import build_application, default_registries
from envoy.app.settings import Settings
from envoy.domain.events import Closed, Outcome, OwnerResolved
from envoy.domain.ids import owner_for_user, owner_stream
from envoy.domain.session import SessionState, fold
from envoy.domain.telegram import (
    BusinessConnection,
    BusinessConnectionUpdate,
    BusinessMessageUpdate,
    CallbackUpdate,
    MessageUpdate,
    WidgetQueryUpdate,
)
from envoy.llm.claude import ClaudeLanguageModel
from envoy.llm.fake import ScriptedLanguageModel
from envoy.llm.translator import IntentWire, MandateDraftWire, WireValue
from envoy.telegram.memory import FakeTransport
from tests.support.harness import message_is
from tests.support.mandates import meeting_mandate

OWNER, COUNTERPART, CONNECTION = 1, 2, "conn"
PHASE1_EVENTS = [
    "SessionOpened",
    "MandateAttested",
    "Disclosed",
    "Decided",
    "Delivered",
    "IntentParsed",
    "Decided",
    "Delivered",
    "IntentParsed",
    "Decided",
    "Escalated",
    "OwnerResolved",
    "Decided",
    "Delivered",
    "ChecklistOpened",
    "CommitmentDone",
    "CommitmentDone",
    "Closed",
]
CASSETTES = json.loads(Path("tests/cassettes/claude_messages.json").read_text("utf-8"))


def settings(**kwargs) -> Settings:
    return Settings(
        _env_file=None,
        agent_key="11" * 32,
        master_key="22" * 32,
        global_rate=10000,
        per_chat_rate=10000,
        owner_ids=frozenset({OWNER}),
        default_timezone="UTC",
        **kwargs,
    )


class Driver:
    def __init__(self, app, transport: FakeTransport) -> None:
        self.app = app
        self.transport = transport
        self.update = 500

    async def feed(self, update) -> None:
        await self.app.pipeline.processor.process(update)

    def next_id(self) -> int:
        self.update += 1
        return self.update

    async def business(self, user: int, text: str | None = None, **extra) -> None:
        identity = self.next_id()
        await self.feed(
            BusinessMessageUpdate(
                identity, CONNECTION, COUNTERPART, user, message_id=identity, text=text, **extra
            )
        )

    async def callback(self, data: str) -> None:
        await self.feed(CallbackUpdate(self.next_id(), f"cb{self.update}", OWNER, OWNER, 1, data))

    async def widget(self, data: str, message_id: int) -> None:
        identity = self.next_id()
        await self.feed(
            WidgetQueryUpdate(
                identity, f"wq{identity}", OWNER, OWNER, message_id, data, datetime.now(UTC)
            )
        )

    async def approve(self, owner_view: str) -> None:
        if owner_view == "text":
            card = next(r for r in reversed(self.transport.sent) if r.reply_markup and r.thread_id)
            await self.callback(card.reply_markup.buttons[0][0].data)
            return
        card = next(r for r in self.transport.widgets.values() if r.allow_queries)
        cause = json.loads(card.state)["cause"]
        await self.widget(f"a:{cause}", card.message.message_id)
        assert self.transport.widget_answers[-1][1] == "ok"

    def counterpart_texts(self) -> list[str]:
        return [r.message.text for r in self.transport.sent if r.business_connection_id]


def build(llm_name: str = "scripted", model=None, **kwargs):
    kwargs.setdefault("journal_store", "memory")
    transport = FakeTransport()
    registries = default_registries()
    registries.senders.register("memory-transport", lambda **_: transport)
    registries.sources.register("memory-transport", lambda sender: sender)
    registries.surfaces.register("memory-transport", lambda sender: sender)
    registries.widgets.register("memory-transport", lambda sender: sender)
    if model is not None:
        registries.models.register(llm_name, lambda **_: model)
    app = build_application(
        settings(telegram_transport="memory-transport", llm=llm_name, **kwargs),
        registries=registries,
    )
    return Driver(app, transport)


@pytest.mark.parametrize("owner_view", ["text", "widget"])
async def test_meeting_scenario_reaches_checklist_and_fulfilled(owner_view) -> None:
    llm = ScriptedLanguageModel()
    driver = build(model=llm, owner_view=owner_view)
    llm.add(
        lambda data: "text" in json.loads(data),
        MandateDraftWire(
            template="meeting",
            time_slots=["2026-12-03T10:00:00+00:00", "2026-12-03T12:00:00+00:00"],
            places=["офис", "кафе"],
            excluded_places=["вокзал"],
            max_rounds=4,
            confirm_before_agreeing=True,
        ),
    )
    llm.add(message_is("Здравствуйте"), IntentWire(kind="unclear"))
    llm.add(
        message_is("Давайте в кафе в 12"),
        IntentWire(
            kind="propose",
            values=[
                WireValue(issue="time", value="2026-12-03T12:00:00+00:00"),
                WireValue(issue="place", value="кафе"),
            ],
        ),
    )
    llm.add(message_is("Ладно, согласен на ваш вариант"), IntentWire(kind="accept"))
    try:
        await driver.feed(MessageUpdate(driver.next_id(), OWNER, OWNER, text="/start"))
        await driver.feed(
            MessageUpdate(
                driver.next_id(), OWNER, OWNER, text="3 декабря в 10 или 12, офис или кафе"
            )
        )
        confirm = driver.transport.sent[-1].reply_markup.buttons[0][0].data
        await driver.callback(confirm)
        await driver.feed(
            BusinessConnectionUpdate(driver.next_id(), BusinessConnection(CONNECTION, OWNER, OWNER))
        )
        await driver.business(COUNTERPART, "Здравствуйте")
        await driver.business(OWNER, "/envoy")
        assert "ИИ-агент" in driver.counterpart_texts()[0]
        await driver.business(COUNTERPART, "Давайте в кафе в 12")
        await driver.business(COUNTERPART, "Ладно, согласен на ваш вариант")
        await driver.approve(owner_view)
        assert driver.counterpart_texts()[-1].startswith("Договорились")
        assert len(driver.transport.checklists) == 1
        await driver.business(OWNER, content_kind="service", done_task_ids=(1,))
        await driver.business(COUNTERPART, content_kind="service", done_task_ids=(2,))
        sessions = driver.app.pipeline.stores.sessions
        record = await sessions.get(next(iter(sessions._records)))
        events = await driver.app.pipeline.stores.journal.events(record.id)
        view = fold(events)
        assert (view.state, view.outcome) == (SessionState.CLOSED, Outcome.fulfilled)
        assert events[-1] == Closed(outcome=Outcome.fulfilled, reason="all_commitments_done")
        resolved = [event for event in events if isinstance(event, OwnerResolved)]
        assert [event.resolution for event in resolved] == ["approved"]
        assert [type(event).__name__ for event in events] == PHASE1_EVENTS
        assert all(
            r.message.chat_id == COUNTERPART
            for r in driver.transport.sent
            if r.business_connection_id
        )
        assert all(r.message.chat_id == OWNER for r in driver.transport.widgets.values())
        assert bool(driver.transport.widgets) is (owner_view == "widget")
        for stream in (record.id, owner_stream(owner_for_user(OWNER))):
            entries = await driver.app.reader.read(stream)
            driver.app.verifier.verify(entries, {entries[0].author: driver.app.public_key})
    finally:
        await driver.app.close()


async def test_llm_implementation_swapped_by_config(aiohttp_server, monkeypatch) -> None:
    calls = []

    async def messages(request: web.Request) -> web.Response:
        calls.append(await request.json())
        return web.json_response(CASSETTES["intent_propose"]["body"])

    app = web.Application()
    app.router.add_post("/v1/messages", messages)
    monkeypatch.setenv("ANTHROPIC_BASE_URL", await aiohttp_server(app))
    for name in ("fake", "anthropic"):
        driver = build(name, **({"anthropic_api_key": "test-key"} if name == "anthropic" else {}))
        try:
            translator_model = driver.app.pipeline.session_flow._translator._model._inner
            expected = ClaudeLanguageModel if name == "anthropic" else ScriptedLanguageModel
            assert isinstance(translator_model, expected)
            owner = owner_for_user(OWNER)
            store = driver.app.pipeline.stores.mandates
            draft = await store.save_draft(owner, meeting_mandate(), b"s" * 32)
            await driver.callback(f"mandate_confirm:{draft.id}")
            await driver.feed(
                BusinessConnectionUpdate(
                    driver.next_id(), BusinessConnection(CONNECTION, OWNER, OWNER)
                )
            )
            await driver.business(COUNTERPART, "привет")
            await driver.business(OWNER, "/envoy")
            before = len(driver.counterpart_texts())
            await driver.business(COUNTERPART, "давай в кафе в 10")
            sent = driver.counterpart_texts()[before:]
            assert len(sent) == (1 if name == "anthropic" else 0)
        finally:
            await driver.app.close()
    assert len(calls) == 1


async def test_widget_state_and_caption_exclude_mandate_canaries() -> None:
    import asyncio
    from dataclasses import asdict

    from structlog.testing import capture_logs

    from tests.support.harness import propose
    from tests.support.mandates import RESERVATION, SLOTS, WEIGHT_PLACE, WEIGHT_TIME

    llm = ScriptedLanguageModel()
    llm.add(message_is("привет"), IntentWire(kind="unclear"))
    llm.add(message_is("давай в парке"), propose(SLOTS[3].isoformat(), "park"))
    llm.add(message_is("согласен"), IntentWire(kind="accept"))
    driver = build(model=llm, owner_view="widget")
    mandate = meeting_mandate(escalation={"always": ["accept_deal"]})
    try:
        with capture_logs() as logs:
            store = driver.app.pipeline.stores.mandates
            draft = await store.save_draft(owner_for_user(OWNER), mandate, b"s" * 32)
            await driver.callback(f"mandate_confirm:{draft.id}")
            await driver.feed(
                BusinessConnectionUpdate(
                    driver.next_id(), BusinessConnection(CONNECTION, OWNER, OWNER)
                )
            )
            await driver.business(COUNTERPART, "привет")
            await driver.business(OWNER, "/envoy")
            await driver.business(COUNTERPART, "давай в парке")
            await driver.business(COUNTERPART, "согласен")
            card = next(r for r in driver.transport.widgets.values() if r.allow_queries)
            cause = json.loads(card.state)["cause"]
            await asyncio.gather(
                driver.widget(f"a:{cause}", card.message.message_id),
                driver.callback(f"approve:{cause}"),
            )
        sessions = driver.app.pipeline.stores.sessions
        record = await sessions.get(next(iter(sessions._records)))
        events = await driver.app.pipeline.stores.journal.events(record.id)
        assert len([event for event in events if isinstance(event, OwnerResolved)]) == 1
        accepts = [text for text in driver.counterpart_texts() if text.startswith("Договорились")]
        assert len(accepts) == 1
        observed = [
            *(asdict(widget) | {"message": None} for widget in driver.transport.widgets.values()),
            *driver.transport.widget_states.values(),
            driver.transport.widget_answers,
            [asdict(sent) for sent in driver.transport.sent],
            driver.transport.callback_answers,
            logs,
        ]
        dumped = json.dumps(observed, ensure_ascii=False, default=str)
        assert "Нужно ваше решение" in dumped
        for canary in (str(RESERVATION), str(WEIGHT_TIME), str(WEIGHT_PLACE), "minimum_utility"):
            assert canary not in dumped
    finally:
        await driver.app.close()
