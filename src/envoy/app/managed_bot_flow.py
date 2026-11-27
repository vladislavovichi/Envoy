import asyncio
import base64
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, replace
from typing import Literal, Protocol

import structlog

from envoy.crypto.aead import ContentCipher
from envoy.domain.ids import OwnerId, owner_for_user
from envoy.domain.telegram import ManagedBotUpdate, Update
from envoy.ports.rng import Rng
from envoy.telegram.ports import BotManager, ManagedBotProfile

logger = structlog.get_logger()
MAX_TRUSTED = 10
DISCLOSURE = "ИИ-агент Envoy: договаривается от имени владельца, итог подтверждает человек."

type ManagedStatus = Literal["requested", "token", "configured", "detached"]
type UpdateHandler = Callable[[Update], Awaitable[None]]


@dataclass(frozen=True)
class ManagedBotRecord:
    owner: OwnerId
    owner_user: int
    request_id: int
    secret: str
    status: ManagedStatus = "requested"
    bot_id: int | None = None
    username: str | None = None
    token: bytes | None = None
    trusted: tuple[int, ...] = ()


class ManagedBotRepository(Protocol):
    async def put(self, record: ManagedBotRecord) -> None: ...
    async def pending(self, owner: OwnerId) -> ManagedBotRecord | None: ...
    async def by_bot(self, bot_id: int) -> ManagedBotRecord | None: ...
    async def all(self) -> list[ManagedBotRecord]: ...


class InMemoryManagedBotRepository:
    def __init__(self) -> None:
        self._records: dict[tuple[OwnerId, int], ManagedBotRecord] = {}

    async def put(self, record: ManagedBotRecord) -> None:
        self._records[(record.owner, record.request_id)] = record

    async def pending(self, owner: OwnerId) -> ManagedBotRecord | None:
        found = [
            record
            for record in self._records.values()
            if record.owner == owner and record.status == "requested"
        ]
        return found[-1] if found else None

    async def by_bot(self, bot_id: int) -> ManagedBotRecord | None:
        return next((record for record in self._records.values() if record.bot_id == bot_id), None)

    async def all(self) -> list[ManagedBotRecord]:
        return list(self._records.values())


@dataclass
class _Running:
    token: str
    client: ManagedBotProfile
    task: asyncio.Task[None]


class ManagedBotFlow:
    def __init__(
        self,
        repo: ManagedBotRepository,
        manager: BotManager,
        clients: Callable[[str, ManagedBotRecord], ManagedBotProfile],
        handlers: Callable[[ManagedBotRecord, ManagedBotProfile], UpdateHandler],
        cipher: ContentCipher,
        master_key: bytes,
        rng: Rng,
        owner_users: frozenset[int],
        fingerprint: str,
    ) -> None:
        self._repo = repo
        self._manager = manager
        self._clients = clients
        self._handlers = handlers
        self._cipher = cipher
        self._key = master_key
        self._rng = rng
        self._owners = owner_users
        self._fingerprint = fingerprint
        self._running: dict[int, _Running] = {}
        self._lock = asyncio.Lock()

    def running(self) -> dict[int, str]:
        return {bot: entry.token for bot, entry in self._running.items() if not entry.task.done()}

    def _seal(self, bot_id: int, token: str) -> bytes:
        return self._cipher.encrypt(self._key, token.encode(), bot_id.to_bytes(8, "big"))

    def _open(self, record: ManagedBotRecord) -> str | None:
        if record.token is None or record.bot_id is None:
            return None
        return self._cipher.decrypt(
            self._key, record.token, record.bot_id.to_bytes(8, "big")
        ).decode()

    async def request(self, owner: OwnerId) -> ManagedBotRecord:
        users = [user for user in self._owners if owner_for_user(user) == owner]
        if not users:
            raise PermissionError("Unknown owner")
        user = users[0]
        record = ManagedBotRecord(
            owner=owner,
            owner_user=user,
            request_id=int.from_bytes(self._rng.token_bytes(4), "big") & 0x7FFFFFFF,
            secret=base64.urlsafe_b64encode(self._rng.token_bytes(24)).decode().rstrip("="),
        )
        await self._repo.put(record)
        await self._manager.request_managed_bot(
            user, record.request_id, "Мой агент Envoy", f"envoy{user}_bot"
        )
        return record

    async def reconcile(self, event: ManagedBotUpdate) -> None:
        async with self._lock:
            await self._reconcile(event)

    async def _reconcile(self, event: ManagedBotUpdate) -> None:
        if event.user_id not in self._owners:
            return
        owner = owner_for_user(event.user_id)
        record = await self._repo.by_bot(event.bot_id)
        if record is not None and record.owner != owner:
            await self._stop(event.bot_id)
            await self._repo.put(replace(record, status="detached", token=None))
            logger.warning("managed_bot_owner_changed", bot=event.bot_id)
            return
        if record is None:
            pending = await self._repo.pending(owner)
            if pending is None:
                return
            record = replace(pending, bot_id=event.bot_id, username=event.bot_username)
        if record.status == "detached":
            return
        assert record.bot_id is not None
        token = await self._manager.managed_bot_token(record.bot_id)
        if record.status == "configured" and self._open(record) == token:
            await self._start(record, token)
            return
        record = replace(record, token=self._seal(record.bot_id, token), status="token")
        await self._repo.put(record)
        await self._configure(record, token)

    async def _configure(self, record: ManagedBotRecord, token: str) -> None:
        assert record.bot_id is not None
        client = self._clients(token, record)
        await client.set_name("Агент Envoy")
        await client.set_description(f"{DISCLOSURE} Ключ агента: {self._fingerprint}")
        await self._manager.set_managed_bot_access(record.bot_id, True, record.trusted)
        configured = replace(record, status="configured")
        await self._repo.put(configured)
        await self._start(configured, token, client)

    async def _start(
        self, record: ManagedBotRecord, token: str, client: ManagedBotProfile | None = None
    ) -> None:
        assert record.bot_id is not None
        current = self._running.get(record.bot_id)
        if current is not None and current.token == token and not current.task.done():
            if client is not None:
                await client.close()
            return
        await self._stop(record.bot_id)
        client = client or self._clients(token, record)
        handler = self._handlers(record, client)
        task = asyncio.create_task(self._loop(record.bot_id, client, handler))
        self._running[record.bot_id] = _Running(token, client, task)

    async def _loop(self, bot_id: int, client: ManagedBotProfile, handler: UpdateHandler) -> None:
        async for update in client.updates():
            try:
                await handler(update)
            except Exception:
                logger.exception("managed_bot_update_failed", bot=bot_id)

    async def _stop(self, bot_id: int) -> None:
        current = self._running.pop(bot_id, None)
        if current is None:
            return
        await current.client.close()
        current.task.cancel()
        try:
            await current.task
        except asyncio.CancelledError, Exception:
            pass

    async def set_access(self, owner: OwnerId, bot_id: int, trusted: Sequence[int]) -> None:
        if len(trusted) > MAX_TRUSTED:
            raise ValueError(f"At most {MAX_TRUSTED} trusted users")
        record = await self._repo.by_bot(bot_id)
        if record is None or record.owner != owner or record.status == "detached":
            raise PermissionError("Bot is not managed for this owner")
        await self._manager.set_managed_bot_access(bot_id, True, list(trusted))
        await self._repo.put(replace(record, trusted=tuple(trusted)))

    async def resume(self) -> None:
        async with self._lock:
            for record in await self._repo.all():
                token = self._open(record)
                if token is None:
                    continue
                if record.status == "token":
                    await self._configure(record, token)
                elif record.status == "configured":
                    await self._start(record, token)

    async def close(self) -> None:
        for bot_id in list(self._running):
            await self._stop(bot_id)
