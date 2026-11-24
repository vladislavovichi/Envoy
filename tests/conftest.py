from datetime import UTC, datetime
from uuid import UUID

import pytest

from envoy.crypto.aead import ContentCipher
from envoy.crypto.keys import SigningKey
from envoy.domain.ids import AgentId, SessionId
from envoy.journal.chain import JournalAuthor
from envoy.journal.memory import InMemoryJournalStore, InMemoryKeyVault
from envoy.ports.clock import FixedClock
from envoy.ports.rng import SeededRng

ALL_TABLES = (
    "journal_entries, entry_keys, mandates, sessions, business_connections, business_chats, "
    "processed_updates, scheduled_jobs, llm_usage"
)


@pytest.fixture
async def aiohttp_server():
    from aiohttp import web

    runners = []

    async def start(app):
        runner = web.AppRunner(app)
        await runner.setup()
        await web.TCPSite(runner, "127.0.0.1", 0).start()
        runners.append(runner)
        return f"http://127.0.0.1:{runner.addresses[0][1]}"

    yield start
    for runner in runners:
        await runner.cleanup()


@pytest.fixture
async def fake_bot_api():
    from tg_fake_api.server import FakeBotApiServer

    server = FakeBotApiServer()
    await server.start()
    try:
        yield server
    finally:
        await server.close()


@pytest.fixture(scope="session")
def pg_url():
    import os
    import secrets

    from testcontainers.community.postgres import PostgresContainer

    configured = os.environ.get("ENVOY_TEST_DATABASE_URL")
    if configured:
        from dotenv import dotenv_values

        from tests.db_safety import guard_test_database

        application_url = os.environ.get("ENVOY_DATABASE_URL") or dotenv_values(".env").get(
            "ENVOY_DATABASE_URL"
        )
        guard_test_database(configured, application_url)
        yield configured
    else:
        with PostgresContainer(
            "postgres:18.7",
            username="envoy",
            password=secrets.token_urlsafe(32),
            dbname="envoy",
            driver="asyncpg",
        ) as container:
            yield container.get_connection_url()


@pytest.fixture
async def pg_engine(pg_url):
    from alembic import command
    from alembic.config import Config
    from sqlalchemy import inspect
    from sqlalchemy.ext.asyncio import create_async_engine
    from sqlalchemy.pool import NullPool

    engine = create_async_engine(pg_url, poolclass=NullPool)
    async with engine.begin() as connection:

        def upgrade(conn):
            if "alembic_version" not in inspect(conn).get_table_names():
                config = Config("alembic.ini")
                config.attributes["connection"] = conn
                command.upgrade(config, "head")

        await connection.run_sync(upgrade)
    try:
        yield engine
    finally:
        await engine.dispose()


@pytest.fixture
async def pg_session_factory(pg_engine):
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import async_sessionmaker

    async with pg_engine.begin() as connection:
        await connection.execute(text(f"TRUNCATE {ALL_TABLES}"))
    yield async_sessionmaker(pg_engine, expire_on_commit=False)
    async with pg_engine.begin() as connection:
        await connection.execute(text(f"TRUNCATE {ALL_TABLES}"))


@pytest.fixture
def journal_parts():
    session = SessionId(UUID(int=1))
    agent = AgentId(UUID(int=2))
    key = SigningKey.generate()
    vault = InMemoryKeyVault()
    store = InMemoryJournalStore(vault)
    cipher = ContentCipher(SeededRng(5))
    clock = FixedClock(datetime(2026, 1, 1, tzinfo=UTC))
    author = JournalAuthor(session, agent, key, store, store, vault, cipher, clock)
    return session, agent, key, store, vault, cipher, clock, author
