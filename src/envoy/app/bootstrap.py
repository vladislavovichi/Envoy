from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from hashlib import sha256
from typing import cast
from uuid import UUID

from anthropic import AsyncAnthropic
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from envoy.app.application import Application
from envoy.app.journal import SessionJournal
from envoy.app.memory import (
    InMemoryBusinessConnectionRepository,
    InMemoryScheduler,
    InMemorySessionRepository,
    InMemoryUpdateLedger,
)
from envoy.app.pipeline import PipelineConfig, Stores, TelegramSurface, assemble_pipeline
from envoy.app.ports import (
    BusinessConnectionRepository,
    Scheduler,
    SessionRepository,
    UpdateLedger,
)
from envoy.app.recovery import Recovery
from envoy.app.runtime import Runtime
from envoy.app.settings import Settings
from envoy.crypto.aead import ContentCipher
from envoy.crypto.keys import SigningKey
from envoy.domain.ids import AgentId, SessionId
from envoy.domain.telegram import ReplyMarkup, SentMessage
from envoy.journal.chain import JournalAuthor
from envoy.journal.memory import InMemoryJournalStore, InMemoryKeyVault
from envoy.journal.ports import JournalCommitter, JournalStore, KeyVault
from envoy.journal.verifier import JournalVerifier
from envoy.llm.budget import InMemoryUsageLedger
from envoy.llm.claude import ClaudeLanguageModel
from envoy.llm.fake import ScriptedLanguageModel
from envoy.llm.ports import LanguageModel, UsageLedger
from envoy.mandate.ports import InMemoryMandateStore, MandateStore
from envoy.observability.logging import configure_logging
from envoy.persistence.journal_store import PgJournalStore
from envoy.persistence.key_vault import PgKeyVault
from envoy.persistence.state_stores import (
    PgBusinessConnectionRepository,
    PgMandateStore,
    PgScheduler,
    PgSessionRepository,
    PgUpdateLedger,
    PgUsageLedger,
)
from envoy.ports.clock import Clock, FixedClock, SystemClock
from envoy.ports.ids import IdGenerator, SequentialIdGenerator, UuidV7Generator
from envoy.ports.registry import Registry
from envoy.ports.rng import Rng, SeededRng, SystemRng
from envoy.telegram.adapter import AiogramAdapter
from envoy.telegram.memory import FakeTransport
from envoy.telegram.outbound import OutboundQueue
from envoy.telegram.ports import MessageSender, UpdateSource

type Resources = list[Callable[[], Awaitable[None]]]


@dataclass(frozen=True)
class Storage:
    store: JournalStore
    vault: KeyVault
    committer: JournalCommitter
    mandates: MandateStore
    sessions: SessionRepository
    connections: BusinessConnectionRepository
    ledger: UpdateLedger
    scheduler: Scheduler
    usage: UsageLedger


@dataclass(frozen=True)
class ApplicationRegistries:
    stores: Registry[Storage]
    senders: Registry[MessageSender]
    sources: Registry[UpdateSource]
    surfaces: Registry[TelegramSurface]
    models: Registry[LanguageModel]
    clocks: Registry[Clock]
    rngs: Registry[Rng]
    ids: Registry[IdGenerator]


class QueuedSender:
    def __init__(self, queue: OutboundQueue) -> None:
        self._queue = queue

    async def send_message(
        self,
        chat_id: int,
        text: str,
        *,
        reply_markup: ReplyMarkup | None = None,
        business_connection_id: str | None = None,
        thread_id: int | None = None,
    ) -> SentMessage:
        return await self._queue.submit(
            chat_id,
            text,
            reply_markup=reply_markup,
            business_connection_id=business_connection_id,
            thread_id=thread_id,
        )


def _stores_registry() -> Registry[Storage]:
    stores = Registry[Storage]()

    def memory(ids: IdGenerator, **kwargs: object) -> Storage:
        vault = InMemoryKeyVault()
        store = InMemoryJournalStore(vault)
        return Storage(
            store,
            vault,
            store,
            InMemoryMandateStore(ids),
            InMemorySessionRepository(),
            InMemoryBusinessConnectionRepository(),
            InMemoryUpdateLedger(),
            InMemoryScheduler(),
            InMemoryUsageLedger(),
        )

    def postgres(
        settings: Settings, cipher: ContentCipher, ids: IdGenerator, resources: Resources
    ) -> Storage:
        if settings.database_url is None:
            raise ValueError("ENVOY_DATABASE_URL is required")
        engine = create_async_engine(settings.database_url.get_secret_value())
        resources.append(engine.dispose)
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        master_key = bytes.fromhex(settings.master_key.get_secret_value())
        vault = PgKeyVault(sessions, master_key, cipher)
        store = PgJournalStore(sessions, vault)
        return Storage(
            store,
            vault,
            store,
            PgMandateStore(sessions, ids, cipher, master_key),
            PgSessionRepository(sessions),
            PgBusinessConnectionRepository(sessions),
            PgUpdateLedger(sessions),
            PgScheduler(sessions),
            PgUsageLedger(sessions),
        )

    stores.register("memory", memory)
    stores.register("postgres", postgres)
    return stores


def _models_registry() -> Registry[LanguageModel]:
    models = Registry[LanguageModel]()
    models.register("fake", lambda **_: ScriptedLanguageModel())

    def anthropic(settings: Settings, resources: Resources) -> LanguageModel:
        if settings.anthropic_api_key is None:
            raise ValueError("ENVOY_ANTHROPIC_API_KEY is required")
        client = AsyncAnthropic(api_key=settings.anthropic_api_key.get_secret_value())
        resources.append(client.close)
        return ClaudeLanguageModel(client, settings.llm_model, timeout=60)

    models.register("anthropic", anthropic)
    return models


def default_registries() -> ApplicationRegistries:
    senders = Registry[MessageSender]()
    senders.register("fake", lambda **_: FakeTransport())

    def aiogram(settings: Settings, resources: Resources) -> MessageSender:
        if settings.bot_token is None:
            raise ValueError("ENVOY_BOT_TOKEN is required")
        adapter = AiogramAdapter(settings.bot_token.get_secret_value(), settings.telegram_api_base)
        resources.append(adapter.close)
        return adapter

    senders.register("aiogram", aiogram)
    sources = Registry[UpdateSource]()
    surfaces = Registry[TelegramSurface]()
    for name in ("fake", "aiogram"):
        sources.register(name, lambda sender: cast(UpdateSource, sender))
        surfaces.register(name, lambda sender: cast(TelegramSurface, sender))
    clocks = Registry[Clock]()
    clocks.register("system", SystemClock)
    clocks.register("fixed", FixedClock)
    rngs = Registry[Rng]()
    rngs.register("system", SystemRng)
    rngs.register("seeded", SeededRng)
    ids = Registry[IdGenerator]()
    ids.register("uuid7", UuidV7Generator)
    ids.register("sequential", SequentialIdGenerator)
    return ApplicationRegistries(
        _stores_registry(), senders, sources, surfaces, _models_registry(), clocks, rngs, ids
    )


def build_application(
    settings: Settings, *, registries: ApplicationRegistries | None = None
) -> Application:
    factories = registries if registries is not None else default_registries()
    resources: Resources = []
    configure_logging()
    rng = factories.rngs.create("system")
    clock = factories.clocks.create("system")
    ids = factories.ids.create("uuid7")
    cipher = ContentCipher(rng)
    signing_key = SigningKey.from_bytes(bytes.fromhex(settings.agent_key.get_secret_value()))
    agent = AgentId(UUID(bytes=sha256(signing_key.public().to_bytes()).digest()[:16]))
    storage = factories.stores.create(
        settings.journal_store, settings=settings, cipher=cipher, ids=ids, resources=resources
    )
    raw_sender = factories.senders.create(
        settings.telegram_transport, settings=settings, resources=resources
    )
    source = factories.sources.create(settings.telegram_transport, sender=raw_sender)
    surface = factories.surfaces.create(settings.telegram_transport, sender=raw_sender)
    model = factories.models.create(settings.llm, settings=settings, resources=resources)
    queue = OutboundQueue(
        raw_sender, settings.global_rate, settings.per_chat_rate, settings.max_retry_after, clock
    )
    sender = QueuedSender(queue)

    def author(session: SessionId) -> JournalAuthor:
        return JournalAuthor(
            session,
            agent,
            signing_key,
            storage.committer,
            storage.store,
            storage.vault,
            cipher,
            clock,
        )

    stores = Stores(
        journal=SessionJournal(author, storage.store, storage.vault, cipher),
        mandates=storage.mandates,
        sessions=storage.sessions,
        connections=storage.connections,
        ledger=storage.ledger,
        scheduler=storage.scheduler,
        usage=storage.usage,
    )
    pipeline = assemble_pipeline(
        PipelineConfig(
            owner_ids=settings.owner_ids,
            timezone=settings.default_timezone,
            agent_name=settings.agent_name,
            inbound_max_text_length=settings.inbound_max_text_length,
            inbound_rate_per_minute=settings.inbound_rate_per_minute,
            llm_daily_token_budget=settings.llm_daily_token_budget,
        ),
        stores,
        sender,
        surface,
        model,
        signing_key,
        clock,
        rng,
    )
    recovery = Recovery(stores.sessions, pipeline.processor, stores.scheduler, clock)
    runtime = Runtime(source, pipeline.processor, stores.scheduler, recovery, clock)

    async def close() -> None:
        await queue.close()
        for cleanup in reversed(resources):
            await cleanup()

    return Application(
        storage.store,
        storage.vault,
        cipher,
        signing_key.public(),
        JournalVerifier(),
        sender,
        source,
        pipeline,
        runtime,
        author,
        close,
    )
