import json
from dataclasses import dataclass, field

from pydantic import BaseModel

from envoy.app.memory import (
    InMemoryBusinessConnectionRepository,
    InMemoryScheduler,
    InMemorySessionRepository,
    InMemoryUpdateLedger,
)
from envoy.app.pipeline import Pipeline, PipelineConfig, Stores, assemble_pipeline
from envoy.domain.events import SessionEvent
from envoy.domain.ids import owner_for_user
from envoy.domain.session import SessionView, fold
from envoy.domain.telegram import (
    BusinessConnection,
    BusinessConnectionUpdate,
    BusinessMessageUpdate,
    CallbackUpdate,
    MessageUpdate,
    TextEntity,
)
from envoy.journal.memory import InMemoryJournalStore
from envoy.llm.budget import InMemoryUsageLedger
from envoy.llm.fake import ScriptedLanguageModel
from envoy.llm.translator import IntentWire, MandateDraftWire, WireValue
from envoy.mandate.ports import InMemoryMandateStore
from envoy.mandate.schema import Mandate
from envoy.ports.clock import FixedClock
from envoy.ports.ids import SequentialIdGenerator
from envoy.ports.rng import SeededRng
from envoy.telegram.memory import FakeTransport
from tests.support.journal import build_journal
from tests.support.mandates import meeting_mandate
from tests.support.sessions import T0

OWNER_USER = 1
COUNTERPART = 2
CONNECTION = "conn"


def message_is(text: str):
    return lambda data: json.loads(data).get("message") == text


def propose(time: str, place: str) -> IntentWire:
    return IntentWire(
        kind="propose",
        values=[WireValue(issue="time", value=time), WireValue(issue="place", value=place)],
    )


@dataclass
class Harness:
    pipeline: Pipeline
    transport: FakeTransport
    llm: ScriptedLanguageModel
    clock: FixedClock
    journal_store: InMemoryJournalStore
    signing_key: object
    next_update: int = 100
    sent_updates: list = field(default_factory=list)

    def update_id(self) -> int:
        self.next_update += 1
        return self.next_update

    async def feed(self, update) -> object:
        self.sent_updates.append(update)
        await self.pipeline.processor.process(update)
        return update

    async def connect(self) -> None:
        await self.feed(
            BusinessConnectionUpdate(
                self.update_id(), BusinessConnection(CONNECTION, OWNER_USER, OWNER_USER)
            )
        )

    async def activate(self, mandate: Mandate | None = None) -> None:
        owner = owner_for_user(OWNER_USER)
        draft = await self.pipeline.stores.mandates.save_draft(
            owner, mandate or meeting_mandate(), b"s" * 32
        )
        await self.feed(
            CallbackUpdate(
                self.update_id(), "cbm", OWNER_USER, OWNER_USER, 1, f"mandate_confirm:{draft.id}"
            )
        )

    def business(self, user_id: int, text: str | None, **extra: object) -> BusinessMessageUpdate:
        values: dict = {
            "update_id": self.update_id(),
            "connection_id": CONNECTION,
            "chat_id": COUNTERPART,
            "user_id": user_id,
            "message_id": self.next_update,
            "text": text,
        }
        values.update(extra)
        return BusinessMessageUpdate(**values)

    async def counterpart(self, text: str, reply: BaseModel | None = None, **extra: object):
        if reply is not None:
            self.llm.add(message_is(text), reply)
        return await self.feed(self.business(COUNTERPART, text, **extra))

    async def owner_in_chat(self, text: str):
        return await self.feed(self.business(OWNER_USER, text))

    async def done(self, user_id: int, *task_ids: int):
        return await self.feed(
            self.business(user_id, None, content_kind="service", done_task_ids=task_ids)
        )

    async def private(self, text: str):
        return await self.feed(MessageUpdate(self.update_id(), OWNER_USER, OWNER_USER, text=text))

    async def callback(self, data: str, user_id: int = OWNER_USER):
        return await self.feed(
            CallbackUpdate(self.update_id(), f"cb{self.next_update}", user_id, OWNER_USER, 1, data)
        )

    async def session(self):
        records = await self.pipeline.stores.sessions.list_active()
        if records:
            return records[0]
        all_records = list(self.pipeline.stores.sessions._records.values())
        return all_records[-1] if all_records else None

    async def events(self) -> list[SessionEvent]:
        record = await self.session()
        return await self.pipeline.stores.journal.events(record.id)

    async def view(self) -> SessionView:
        return fold(await self.events())

    def to_counterpart(self) -> list[str]:
        return [
            record.message.text
            for record in self.transport.sent
            if record.business_connection_id == CONNECTION
        ]

    def to_owner(self) -> list[str]:
        return [
            record.message.text
            for record in self.transport.sent
            if record.business_connection_id is None
        ]

    async def run_due_jobs(self) -> None:
        for job in await self.pipeline.stores.scheduler.due(self.clock.now()):
            await self.pipeline.processor.run_job(job)


def build_harness(
    *,
    topics: bool = True,
    owner_ids: frozenset[int] = frozenset({OWNER_USER}),
    budget: int = 1_000_000,
    model=None,
) -> Harness:
    clock = FixedClock(T0)
    journal, store, key = build_journal(clock)
    transport = FakeTransport(topics_available=topics)
    llm = ScriptedLanguageModel()
    stores = Stores(
        journal=journal,
        mandates=InMemoryMandateStore(SequentialIdGenerator()),
        sessions=InMemorySessionRepository(),
        connections=InMemoryBusinessConnectionRepository(),
        ledger=InMemoryUpdateLedger(),
        scheduler=InMemoryScheduler(),
        usage=InMemoryUsageLedger(),
    )
    pipeline = assemble_pipeline(
        PipelineConfig(owner_ids=owner_ids, timezone="UTC", llm_daily_token_budget=budget),
        stores,
        transport,
        transport,
        model or llm,
        key,
        clock,
        SeededRng(9),
    )
    return Harness(pipeline, transport, llm, clock, store, key)


__all__ = [
    "COUNTERPART",
    "CONNECTION",
    "OWNER_USER",
    "Harness",
    "MandateDraftWire",
    "TextEntity",
    "build_harness",
    "message_is",
    "propose",
]
