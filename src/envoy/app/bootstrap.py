from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from hashlib import sha256
from pathlib import Path
from typing import cast
from uuid import UUID

from aiohttp import web
from anthropic import AsyncAnthropic
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from envoy.app.agent_flow import AgentFlow
from envoy.app.agent_ports import InMemoryProtocolRepository, ProtocolRepository
from envoy.app.application import Application
from envoy.app.invitations import InMemoryInvitationRepository, InvitationRepository
from envoy.app.journal import SessionJournal
from envoy.app.managed_bot_flow import (
    InMemoryManagedBotRepository,
    ManagedBotFlow,
    ManagedBotRecord,
    ManagedBotRepository,
    UpdateHandler,
)
from envoy.app.mandate_widget import InMemoryMandateCardStore, MandateCardStore
from envoy.app.memory import (
    InMemoryBusinessConnectionRepository,
    InMemoryOwnerViewStore,
    InMemoryScheduler,
    InMemorySessionRepository,
    InMemoryUpdateLedger,
)
from envoy.app.miniapp import OwnerApi
from envoy.app.owner_signing import InMemoryOwnerSigningStore, OwnerSigningStore
from envoy.app.owner_view import OwnerView, TextOwnerView
from envoy.app.pipeline import (
    AgentWiring,
    Pipeline,
    PipelineConfig,
    Stores,
    TelegramSurface,
    assemble_pipeline,
)
from envoy.app.ports import (
    BusinessConnectionRepository,
    OwnerViewStore,
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
from envoy.domain.telegram import BotMessageUpdate, ReplyMarkup, SentMessage, Update
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
from envoy.persistence.invitation_repo import PgInvitationRepository
from envoy.persistence.journal_store import PgJournalStore
from envoy.persistence.key_registry import PgKeyRegistry
from envoy.persistence.key_vault import PgKeyVault
from envoy.persistence.managed_bot_repo import PgManagedBotRepository
from envoy.persistence.owner_signing import PgOwnerSigningStore
from envoy.persistence.owner_views import PgMandateCardStore, PgOwnerViewStore
from envoy.persistence.protocol_repo import PgProtocolRepository
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
from envoy.protocol.delegation import InMemoryKeyRegistry, KeyRegistry, fingerprint_description
from envoy.protocol.limits import ProtocolLimits
from envoy.telegram.adapter import AiogramAdapter
from envoy.telegram.memory import FakeTransport
from envoy.telegram.outbound import OutboundQueue, QueuedWidgetTransport
from envoy.telegram.ports import (
    BotManager,
    ManagedBotProfile,
    MessageSender,
    UpdateSource,
    WidgetTransport,
)
from envoy.widgets.owner_view import WidgetOwnerView

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
    owner_views: OwnerViewStore
    protocol: ProtocolRepository
    keys: KeyRegistry
    signing: OwnerSigningStore
    invitations: InvitationRepository
    mandate_cards: MandateCardStore
    managed: ManagedBotRepository


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
    widgets: Registry[WidgetTransport] = field(default_factory=Registry[WidgetTransport])
    owner_views: Registry[OwnerView] = field(default_factory=Registry[OwnerView])


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
        mandates = InMemoryMandateStore(ids)
        return Storage(
            store,
            vault,
            store,
            mandates,
            InMemorySessionRepository(),
            InMemoryBusinessConnectionRepository(),
            InMemoryUpdateLedger(),
            InMemoryScheduler(),
            InMemoryUsageLedger(),
            InMemoryOwnerViewStore(),
            InMemoryProtocolRepository(),
            InMemoryKeyRegistry(),
            InMemoryOwnerSigningStore(mandates),
            InMemoryInvitationRepository(),
            InMemoryMandateCardStore(),
            InMemoryManagedBotRepository(),
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
        mandates = PgMandateStore(sessions, ids, cipher, master_key)
        return Storage(
            store,
            vault,
            store,
            mandates,
            PgSessionRepository(sessions),
            PgBusinessConnectionRepository(sessions),
            PgUpdateLedger(sessions),
            PgScheduler(sessions),
            PgUsageLedger(sessions),
            PgOwnerViewStore(sessions),
            PgProtocolRepository(sessions, cipher, master_key),
            PgKeyRegistry(sessions),
            PgOwnerSigningStore(sessions, mandates),
            PgInvitationRepository(sessions),
            PgMandateCardStore(sessions),
            PgManagedBotRepository(sessions),
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
    widgets = Registry[WidgetTransport]()
    for name in ("fake", "aiogram"):
        sources.register(name, lambda sender: cast(UpdateSource, sender))
        surfaces.register(name, lambda sender: cast(TelegramSurface, sender))
        widgets.register(name, lambda sender: cast(WidgetTransport, sender))
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
        _stores_registry(),
        senders,
        sources,
        surfaces,
        _models_registry(),
        clocks,
        rngs,
        ids,
        widgets,
        _owner_views_registry(),
    )


def _owner_views_registry() -> Registry[OwnerView]:
    views = Registry[OwnerView]()

    def text(text: TextOwnerView, **_: object) -> OwnerView:
        return text

    def widget(
        text: TextOwnerView,
        widgets: WidgetTransport,
        sessions: SessionRepository,
        store: OwnerViewStore,
        clock: Clock,
        settings: Settings,
    ) -> OwnerView:
        return WidgetOwnerView(widgets, text, sessions, store, clock, settings.widget_topic_links)

    views.register("text", text)
    views.register("widget", widget)
    return views


def _agent_wiring(
    settings: Settings, storage: Storage, signing_key: SigningKey
) -> AgentWiring | None:
    if not settings.agent_protocol:
        return None
    bot_id = settings.agent_bot_id
    if bot_id is None and settings.bot_token is not None:
        prefix = settings.bot_token.get_secret_value().split(":", 1)[0]
        bot_id = int(prefix) if prefix.isdigit() else None
    owner = settings.agent_owner_id
    if owner is None and len(settings.owner_ids) == 1:
        owner = next(iter(settings.owner_ids))
    if bot_id is None or owner is None:
        raise ValueError("Agent protocol needs ENVOY_AGENT_BOT_ID and ENVOY_AGENT_OWNER_ID")
    limits = ProtocolLimits(
        per_sender_per_minute=settings.agent_sender_rate,
        per_pair_per_minute=settings.agent_pair_rate,
        per_session_per_minute=settings.agent_session_rate,
        retry_interval=timedelta(seconds=settings.agent_retry_seconds),
    )
    return AgentWiring(
        storage.protocol, storage.keys, storage.signing, signing_key, bot_id, owner, limits
    )


def _startup(
    pipeline: Pipeline, surface: object, signing_key: SigningKey
) -> list[Callable[[], Awaitable[None]]]:
    setter = getattr(surface, "set_description", None)
    if pipeline.agent_flow is None or setter is None:
        return []
    text = fingerprint_description(signing_key.public())

    async def publish() -> None:
        await setter(text)

    return [publish]


def _managed(
    settings: Settings,
    storage: Storage,
    pipeline: Pipeline,
    raw_sender: MessageSender,
    cipher: ContentCipher,
    signing_key: SigningKey,
    rng: Rng,
    ticks: list[Callable[[datetime], Awaitable[None]]],
) -> ManagedBotFlow | None:
    factory = pipeline.agent_factory
    if factory is None or not hasattr(raw_sender, "request_managed_bot"):
        return None
    flows: dict[int, AgentFlow] = {}
    base = settings.telegram_api_base

    def clients(token: str, record: ManagedBotRecord) -> ManagedBotProfile:
        return AiogramAdapter(token, base)

    def handlers(record: ManagedBotRecord, client: ManagedBotProfile) -> UpdateHandler:
        assert record.bot_id is not None
        bot_id = record.bot_id
        flow = factory(bot_id, record.owner_user, cast(MessageSender, client))
        flows[bot_id] = flow

        async def handle(update: Update) -> None:
            if isinstance(update, BotMessageUpdate):
                await flow.receive(replace(update, bot_id=bot_id))

        return handle

    async def tick(now: datetime) -> None:
        for flow in list(flows.values()):
            await flow.run_due(now)

    ticks.append(tick)
    return ManagedBotFlow(
        storage.managed,
        cast(BotManager, raw_sender),
        clients,
        handlers,
        cipher,
        bytes.fromhex(settings.master_key.get_secret_value()),
        rng,
        settings.owner_ids,
        signing_key.public().fingerprint(),
    )


def _miniapp(
    settings: Settings,
    storage: Storage,
    pipeline: Pipeline,
    signing_key: SigningKey,
    agent: AgentId,
    clock: Clock,
    rng: Rng,
    resources: Resources,
) -> list[Callable[[], Awaitable[None]]]:
    if settings.miniapp_port is None or settings.bot_token is None:
        return []
    flow = pipeline.agent_flow
    api = OwnerApi(
        storage.signing,
        storage.mandates,
        clock,
        rng,
        settings.bot_token.get_secret_value(),
        settings.owner_ids,
        agent_key=signing_key.public().to_bytes(),
        agent_id=agent,
        bot_id=0 if flow is None else flow.bot_id,
        escalations=flow,
    )
    host, port = settings.miniapp_host, settings.miniapp_port
    static = settings.miniapp_static

    async def serve() -> None:
        runner = web.AppRunner(api.app(Path(static)))
        await runner.setup()
        await web.TCPSite(runner, host, port).start()
        resources.append(runner.cleanup)

    return [serve]


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
    widgets = (
        QueuedWidgetTransport(
            factories.widgets.create(settings.telegram_transport, sender=raw_sender), queue
        )
        if settings.owner_view == "widget"
        else None
    )

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
        owner_views=storage.owner_views,
        invitations=storage.invitations,
        mandate_cards=storage.mandate_cards,
    )

    def owner_view(text: TextOwnerView) -> OwnerView:
        return factories.owner_views.create(
            settings.owner_view,
            text=text,
            widgets=widgets,
            sessions=storage.sessions,
            store=storage.owner_views,
            clock=clock,
            settings=settings,
        )

    pipeline = assemble_pipeline(
        PipelineConfig(
            owner_ids=settings.owner_ids,
            timezone=settings.default_timezone,
            agent_name=settings.agent_name,
            inbound_max_text_length=settings.inbound_max_text_length,
            inbound_rate_per_minute=settings.inbound_rate_per_minute,
            llm_daily_token_budget=settings.llm_daily_token_budget,
            summary_hour=settings.summary_hour,
            miniapp_url=settings.miniapp_url,
        ),
        stores,
        sender,
        surface,
        model,
        signing_key,
        clock,
        rng,
        owner_view,
        widgets,
        _agent_wiring(settings, storage, signing_key),
    )
    ticks: list[Callable[[datetime], Awaitable[None]]] = []
    startup: list[Callable[[], Awaitable[None]]] = []
    managed = _managed(settings, storage, pipeline, raw_sender, cipher, signing_key, rng, ticks)
    if managed is not None:
        pipeline.processor.managed = managed
        startup.append(managed.resume)
        resources.append(managed.close)
    recovery = Recovery(stores.sessions, pipeline.processor, stores.scheduler, clock)
    runtime = Runtime(
        source,
        pipeline.processor,
        stores.scheduler,
        recovery,
        clock,
        daily_summary=pipeline.daily_summary,
        agents=pipeline.agent_flow,
        startup=[
            *_startup(pipeline, surface, signing_key),
            *_miniapp(settings, storage, pipeline, signing_key, agent, clock, rng, resources),
            *startup,
        ],
        ticks=ticks,
    )

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
