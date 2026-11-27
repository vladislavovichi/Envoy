import asyncio
import base64
import json
import os
from collections.abc import Awaitable, Callable
from uuid import UUID

from envoy.app.bootstrap import build_application
from envoy.app.miniapp import OwnerApi
from envoy.app.settings import Settings
from envoy.crypto.keys import SigningKey
from envoy.domain.events import ChecklistOpened, PeerMessage
from envoy.domain.ids import AgentId, owner_for_user
from envoy.ports.clock import SystemClock
from envoy.ports.rng import SystemRng
from envoy.protocol.envelope import decode_message
from envoy.protocol.machine import Phase
from tests.support.federation import mirrored_mandate, primary_mandate

A_BOT, B_BOT = 501, 502
A_OWNER, B_OWNER = 1, 2


async def database_urls(request) -> tuple[str, str]:
    configured = os.environ.get("ENVOY_FEDERATION_DATABASE_A"), os.environ.get(
        "ENVOY_FEDERATION_DATABASE_B"
    )
    if all(configured):
        urls = (str(configured[0]), str(configured[1]))
    else:
        pg_url = request.getfixturevalue("pg_url")
        from sqlalchemy import text
        from sqlalchemy.ext.asyncio import create_async_engine

        engine = create_async_engine(pg_url, isolation_level="AUTOCOMMIT")
        async with engine.connect() as connection:
            for name in ("federation_a", "federation_b"):
                await connection.execute(text(f"DROP DATABASE IF EXISTS {name} WITH (FORCE)"))
                await connection.execute(text(f"CREATE DATABASE {name}"))
        await engine.dispose()
        base = pg_url.rsplit("/", 1)[0]
        urls = (f"{base}/federation_a", f"{base}/federation_b")
    for url in urls:
        await migrate(url)
    return urls


async def migrate(url: str) -> None:
    from alembic import command
    from alembic.config import Config
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import create_async_engine
    from sqlalchemy.pool import NullPool

    engine = create_async_engine(url, poolclass=NullPool)
    async with engine.begin() as connection:
        await connection.execute(text("DROP SCHEMA public CASCADE"))
        await connection.execute(text("CREATE SCHEMA public"))

        def upgrade(conn) -> None:
            config = Config("alembic.ini")
            config.attributes["connection"] = conn
            command.upgrade(config, "head")

        await connection.run_sync(upgrade)
    await engine.dispose()


def settings(url: str, bot: int, owner: int, seed: int, api: str) -> Settings:
    return Settings(
        _env_file=None,
        journal_store="postgres",
        database_url=url,
        telegram_transport="aiogram",
        bot_token=f"{bot}:token-{bot}",
        telegram_api_base=api,
        agent_key=f"{seed:02x}" * 32,
        master_key=f"{seed + 1:02x}" * 32,
        owner_ids=frozenset({owner}),
        default_timezone="UTC",
        agent_protocol=True,
        agent_session_rate=600,
        agent_pair_rate=600,
        agent_sender_rate=600,
        agent_retry_seconds=0.5,
    )


class Operator:
    def __init__(self, settings: Settings, owner: int, mandate) -> None:
        self.settings = settings
        self.owner = owner
        self.mandate = mandate
        self.app = build_application(settings)
        self.task: asyncio.Task | None = None

    @property
    def flow(self):
        return self.app.pipeline.agent_flow

    async def sign_up(self) -> None:
        owner_key = SigningKey.from_bytes(bytes([self.owner]) * 32)
        api = OwnerApi(
            self.flow._delegations,
            self.app.pipeline.stores.mandates,
            SystemClock(),
            SystemRng(),
            self.settings.bot_token.get_secret_value(),
            self.settings.owner_ids,
            agent_key=self.app.public_key.to_bytes(),
            agent_id=AgentId(UUID(int=self.owner)),
            bot_id=self.flow.bot_id,
        )
        owner = owner_for_user(self.owner)
        public = base64.urlsafe_b64encode(owner_key.public().to_bytes()).decode().rstrip("=")
        for purpose, params in (
            ("enroll", {"public_key": public}),
            ("mandate", {"body": json.loads(self.mandate.model_dump_json())}),
            ("delegate", {}),
        ):
            challenge = await api.create_challenge(owner, purpose, params)
            await api.confirm(owner, challenge.id, owner_key.sign(challenge.bytes_to_sign))

    def start(self) -> None:
        self.task = asyncio.create_task(self.app.runtime.run())

    async def stop(self) -> None:
        assert self.task is not None
        self.task.cancel()
        try:
            await self.task
        except asyncio.CancelledError:
            pass
        await self.app.close()

    async def sessions(self, peer: int):
        return await self.flow._repo.active_with(self.flow.bot_id, peer)


async def eventually(check: Callable[[], Awaitable[bool]], seconds: float = 30) -> None:
    for _ in range(int(seconds * 10)):
        if await check():
            return
        await asyncio.sleep(0.1)
    raise AssertionError("condition not reached in time")


async def test_two_operators_agree_commit_and_survive_restart(request, fake_bot_api) -> None:
    url_a, url_b = await database_urls(request)
    for bot in (A_BOT, B_BOT):
        fake_bot_api.register_bot(f"{bot}:token-{bot}")
    fake_bot_api.drop_bot_messages.append(
        lambda sender, target, text: sender == A_BOT
        and decode_message(text).envelope.type == "commit"
        and decode_message(text).envelope.body.get("stage") == "decision"
    )
    a = Operator(
        settings(url_a, A_BOT, A_OWNER, 0x11, fake_bot_api.base_url), A_OWNER, primary_mandate()
    )
    b = Operator(
        settings(url_b, B_BOT, B_OWNER, 0x33, fake_bot_api.base_url), B_OWNER, mirrored_mandate()
    )
    for operator in (a, b):
        await operator.sign_up()
        operator.start()
    try:
        fake_bot_api.inject_for_bot(
            A_BOT,
            {
                "message": {
                    "message_id": 1,
                    "date": 1,
                    "chat": {"id": A_OWNER, "type": "private"},
                    "from": {"id": A_OWNER, "is_bot": False, "first_name": "A"},
                    "text": f"/agent {B_BOT}",
                }
            },
        )

        async def committing() -> bool:
            sessions = await b.sessions(A_BOT)
            return bool(sessions) and sessions[0].phase is Phase.COMMITTING

        await eventually(committing)
        await b.stop()
        b = Operator(b.settings, B_OWNER, b.mandate)
        b.start()

        async def executing() -> bool:
            left, right = await a.sessions(B_BOT), await b.sessions(A_BOT)
            return bool(left and right) and {left[0].phase, right[0].phase} == {Phase.EXECUTING}

        await eventually(executing)
        left, right = (await a.sessions(B_BOT))[0], (await b.sessions(A_BOT))[0]
        assert left.id == right.id
        assert left.state.agreed == right.state.agreed
        for operator, peer in ((a, b), (b, a)):
            events = await operator.app.pipeline.stores.journal.events(left.id)
            assert len([event for event in events if isinstance(event, ChecklistOpened)]) == 1
            incoming = [
                e.text for e in events if isinstance(e, PeerMessage) and e.direction == "in"
            ]
            assert all(decode_message(text).verify(peer.app.public_key) for text in incoming)
            entries = await operator.app.reader.read(left.id)
            operator.app.verifier.verify(entries, {entries[0].author: operator.app.public_key})
        assert {sender for sender, _, _ in fake_bot_api.bot_traffic} == {A_BOT, B_BOT}
    finally:
        for operator in (a, b):
            if operator.task is not None and not operator.task.done():
                await operator.stop()
