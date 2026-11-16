from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from hashlib import sha256
from typing import cast
from uuid import UUID

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from envoy.app.application import Application
from envoy.app.settings import Settings
from envoy.crypto.aead import ContentCipher
from envoy.crypto.keys import SigningKey
from envoy.domain.ids import AgentId, SessionId
from envoy.domain.telegram import ReplyMarkup, SentMessage
from envoy.journal.chain import JournalAuthor
from envoy.journal.memory import InMemoryJournalStore, InMemoryKeyVault
from envoy.journal.ports import JournalCommitter, JournalStore, KeyVault
from envoy.journal.verifier import JournalVerifier
from envoy.observability.logging import configure_logging
from envoy.persistence.journal_store import PgJournalStore
from envoy.persistence.key_vault import PgKeyVault
from envoy.ports.clock import Clock, FixedClock, SystemClock
from envoy.ports.ids import IdGenerator, SequentialIdGenerator, UuidV7Generator
from envoy.ports.registry import Registry
from envoy.ports.rng import Rng, SeededRng, SystemRng
from envoy.telegram.adapter import AiogramAdapter
from envoy.telegram.memory import FakeTransport
from envoy.telegram.outbound import OutboundQueue
from envoy.telegram.ports import MessageSender, UpdateSource


@dataclass(frozen=True)
class Storage:
    store: JournalStore
    vault: KeyVault
    committer: JournalCommitter


@dataclass(frozen=True)
class ApplicationRegistries:
    stores: Registry[Storage]
    senders: Registry[MessageSender]
    sources: Registry[UpdateSource]
    clocks: Registry[Clock]
    rngs: Registry[Rng]
    ids: Registry[IdGenerator]


class QueuedSender:
    def __init__(self, queue: OutboundQueue) -> None:
        self._queue = queue

    async def send_message(
        self, chat_id: int, text: str, *, reply_markup: ReplyMarkup | None = None
    ) -> SentMessage:
        return await self._queue.submit(chat_id, text, reply_markup=reply_markup)


def default_registries() -> ApplicationRegistries:
    stores = Registry[Storage]()

    def memory(**kwargs: object) -> Storage:
        vault = InMemoryKeyVault()
        store = InMemoryJournalStore(vault)
        return Storage(store, vault, store)

    stores.register("memory", memory)

    def postgres(
        settings: Settings, cipher: ContentCipher, resources: list[Callable[[], Awaitable[None]]]
    ) -> Storage:
        if settings.database_url is None:
            raise ValueError("ENVOY_DATABASE_URL is required")
        engine = create_async_engine(settings.database_url.get_secret_value())
        resources.append(engine.dispose)
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        vault = PgKeyVault(sessions, bytes.fromhex(settings.master_key.get_secret_value()), cipher)
        store = PgJournalStore(sessions, vault)
        return Storage(store, vault, store)

    stores.register("postgres", postgres)
    senders = Registry[MessageSender]()
    senders.register("fake", lambda **_: FakeTransport())

    def aiogram(
        settings: Settings, resources: list[Callable[[], Awaitable[None]]]
    ) -> MessageSender:
        if settings.bot_token is None:
            raise ValueError("ENVOY_BOT_TOKEN is required")
        adapter = AiogramAdapter(settings.bot_token.get_secret_value(), settings.telegram_api_base)
        resources.append(adapter.close)
        return adapter

    senders.register("aiogram", aiogram)
    sources = Registry[UpdateSource]()
    sources.register("fake", lambda sender: cast(UpdateSource, sender))
    sources.register("aiogram", lambda sender: cast(UpdateSource, sender))
    clocks = Registry[Clock]()
    clocks.register("system", SystemClock)
    clocks.register("fixed", FixedClock)
    rngs = Registry[Rng]()
    rngs.register("system", SystemRng)
    rngs.register("seeded", SeededRng)
    ids = Registry[IdGenerator]()
    ids.register("uuid7", UuidV7Generator)
    ids.register("sequential", SequentialIdGenerator)
    return ApplicationRegistries(stores, senders, sources, clocks, rngs, ids)


def build_application(
    settings: Settings, *, registries: ApplicationRegistries | None = None
) -> Application:
    factories = registries if registries is not None else default_registries()
    resources: list[Callable[[], Awaitable[None]]] = []
    configure_logging()
    rng = factories.rngs.create("system")
    clock = factories.clocks.create("system")
    cipher = ContentCipher(rng)
    signing_key = SigningKey.from_bytes(bytes.fromhex(settings.agent_key.get_secret_value()))
    agent = AgentId(UUID(bytes=sha256(signing_key.public().to_bytes()).digest()[:16]))
    storage = factories.stores.create(
        settings.journal_store, settings=settings, cipher=cipher, resources=resources
    )
    raw_sender = factories.senders.create(
        settings.telegram_transport, settings=settings, resources=resources
    )
    source = factories.sources.create(settings.telegram_transport, sender=raw_sender)
    queue = OutboundQueue(
        raw_sender, settings.global_rate, settings.per_chat_rate, settings.max_retry_after, clock
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
        QueuedSender(queue),
        source,
        author,
        close,
    )
