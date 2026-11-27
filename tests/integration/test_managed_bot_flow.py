import asyncio

import pytest
from structlog.testing import capture_logs

from envoy.app.managed_bot_flow import InMemoryManagedBotRepository, ManagedBotFlow
from envoy.crypto.aead import ContentCipher
from envoy.domain.ids import owner_for_user
from envoy.domain.telegram import ManagedBotUpdate, UnsupportedUpdate
from envoy.ports.rng import SeededRng

OWNER, OTHER, STRANGER = 1, 2, 99
BOT = 9001


class SimulatedCrash(BaseException):
    pass


class Manager:
    def __init__(self) -> None:
        self.tokens = {BOT: "9001:secret-token-A"}
        self.requests: list[tuple[int, int]] = []
        self.access: list[tuple[int, bool, list[int]]] = []

    async def request_managed_bot(self, chat_id, request_id, name, username):
        self.requests.append((chat_id, request_id))

    async def managed_bot_token(self, bot_id):
        return self.tokens[bot_id]

    async def replace_managed_bot_token(self, bot_id):
        self.tokens[bot_id] += "-new"
        return self.tokens[bot_id]

    async def set_managed_bot_access(self, bot_id, restricted, user_ids):
        if len(user_ids) > 10:
            raise ValueError("too many")
        self.access.append((bot_id, restricted, list(user_ids)))


class Client:
    crash_next = False

    def __init__(self, token: str) -> None:
        self.token = token
        self.profile: dict[str, str] = {}
        self.closed = False
        self.queue: asyncio.Queue = asyncio.Queue()

    async def set_name(self, name):
        if Client.crash_next:
            Client.crash_next = False
            raise SimulatedCrash
        self.profile["name"] = name

    async def set_description(self, text):
        self.profile["description"] = text

    async def updates(self):
        while not self.closed:
            yield await self.queue.get()

    async def close(self):
        self.closed = True


def build(repo=None, manager=None, cipher=None):
    clients: list[Client] = []
    handled: list = []

    def client_for(token, record):
        client = Client(token)
        clients.append(client)
        return client

    def handler_for(record, client):
        async def handle(update):
            handled.append((record.bot_id, update))

        return handle

    cipher = cipher or ContentCipher(SeededRng(1))
    flow = ManagedBotFlow(
        repo or InMemoryManagedBotRepository(),
        manager or Manager(),
        client_for,
        handler_for,
        cipher,
        bytes(32),
        SeededRng(2),
        frozenset({OWNER, OTHER}),
        "abcd1234",
    )
    return flow, clients, handled


def event(user=OWNER, bot=BOT) -> ManagedBotUpdate:
    return ManagedBotUpdate(1, user, bot, "envoy1_bot")


async def test_repeated_creation_runs_a_single_configured_runtime() -> None:
    flow, clients, handled = build()
    await flow.request(owner_for_user(OWNER))
    await flow.reconcile(event())
    await flow.reconcile(event())
    assert flow.running() == {BOT: "9001:secret-token-A"}
    live = [client for client in clients if not client.closed]
    assert len(live) == 1
    assert "ИИ-агент" in live[0].profile["description"]
    assert "abcd1234" in live[0].profile["description"]
    live[0].queue.put_nowait(UnsupportedUpdate(5))
    await asyncio.sleep(0)
    assert handled == [(BOT, UnsupportedUpdate(5))]
    await flow.close()


async def test_trusted_users_limit_and_owner_checks() -> None:
    flow, *_ = build()
    owner = owner_for_user(OWNER)
    await flow.request(owner)
    await flow.reconcile(event())
    await flow.set_access(owner, BOT, list(range(10)))
    with pytest.raises(ValueError, match="10"):
        await flow.set_access(owner, BOT, list(range(11)))
    with pytest.raises(PermissionError):
        await flow.set_access(owner_for_user(OTHER), BOT, [5])
    await flow.close()


async def test_foreign_unrequested_and_transferred_bots_do_not_change_profile() -> None:
    flow, clients, _ = build()
    await flow.reconcile(event(user=STRANGER))
    await flow.reconcile(event(user=OTHER))
    assert flow.running() == {}
    assert clients == []
    await flow.request(owner_for_user(OWNER))
    await flow.reconcile(event())
    await flow.reconcile(event(user=OTHER))
    assert flow.running() == {}
    assert all(client.closed for client in clients)
    await flow.reconcile(event())
    assert flow.running() == {}
    await flow.close()


async def test_crash_after_token_received_is_configured_on_resume() -> None:
    repo = InMemoryManagedBotRepository()
    cipher = ContentCipher(SeededRng(1))
    flow, _, _ = build(repo, cipher=cipher)
    await flow.request(owner_for_user(OWNER))
    Client.crash_next = True
    with pytest.raises(SimulatedCrash):
        await flow.reconcile(event())
    assert (await repo.by_bot(BOT)).status == "token"
    restarted, clients, _ = build(repo, cipher=cipher)
    await restarted.resume()
    assert (await repo.by_bot(BOT)).status == "configured"
    assert restarted.running() == {BOT: "9001:secret-token-A"}
    assert clients[0].profile["name"] == "Агент Envoy"
    await restarted.close()


async def test_token_change_stops_old_receiver_and_token_never_logged() -> None:
    manager = Manager()
    repo = InMemoryManagedBotRepository()
    flow, clients, _ = build(repo, manager)
    with capture_logs() as logs:
        await flow.request(owner_for_user(OWNER))
        await flow.reconcile(event())
        await manager.replace_managed_bot_token(BOT)
        await flow.reconcile(event())
        await flow.reconcile(event(user=OTHER))
    assert clients[0].closed
    assert clients[-1].token.endswith("-new")
    record = await repo.by_bot(BOT)
    assert b"secret-token" not in (record.token or b"")
    assert "secret-token" not in repr(logs)
    await flow.close()


@pytest.fixture(params=["memory", "postgres"])
def repo(request):
    if request.param == "memory":
        return InMemoryManagedBotRepository()
    from envoy.persistence.managed_bot_repo import PgManagedBotRepository

    return PgManagedBotRepository(request.getfixturevalue("pg_session_factory"))


async def test_repository_round_trip_and_queries(repo) -> None:
    from dataclasses import replace

    from envoy.app.managed_bot_flow import ManagedBotRecord

    owner = owner_for_user(OWNER)
    first = ManagedBotRecord(owner, OWNER, 10, "s1")
    second = ManagedBotRecord(owner, OWNER, 11, "s2")
    await repo.put(first)
    await repo.put(second)
    assert (await repo.pending(owner)).request_id == 11
    bound = replace(second, status="configured", bot_id=BOT, username="b", token=b"x", trusted=(5,))
    await repo.put(bound)
    assert await repo.by_bot(BOT) == bound
    assert (await repo.pending(owner)).request_id == 10
    assert await repo.pending(owner_for_user(OTHER)) is None
    assert {record.request_id for record in await repo.all()} == {10, 11}
