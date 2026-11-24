import asyncio

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import inspect, text

from envoy.domain.errors import ContentErased, SequenceConflict
from envoy.domain.events import JournalEntryType
from envoy.journal.contract import JournalStoreContract
from envoy.persistence.journal_store import PgJournalStore
from envoy.persistence.key_vault import PgKeyVault


class TestPgJournalStore(JournalStoreContract):
    @pytest.fixture
    def store(self, pg_session_factory):
        return PgJournalStore(pg_session_factory)


async def test_erased_key_row_removed(pg_session_factory, journal_parts) -> None:
    session, _, _, _, _, cipher, _, _ = journal_parts
    vault = PgKeyVault(pg_session_factory, cipher.generate_key(), cipher)
    await vault.put(session, 0, b"secret key")
    assert await vault.entry_key(session, 0) == b"secret key"
    await vault.erase(session, 0)
    await vault.erase(session, 0)
    with pytest.raises(ContentErased):
        await vault.entry_key(session, 0)
    async with pg_session_factory() as db:
        assert (await db.execute(text("SELECT count(*) FROM entry_keys"))).scalar_one() == 0


async def test_unique_violation_maps_to_sequence_conflict(
    pg_session_factory, journal_parts
) -> None:
    entry = await journal_parts[-1].record(JournalEntryType.decided, b"data", b"c")
    store = PgJournalStore(pg_session_factory)
    results = await asyncio.gather(store.append(entry), store.append(entry), return_exceptions=True)
    assert sum(result is None for result in results) == 1
    assert sum(isinstance(result, SequenceConflict) for result in results) == 1


async def test_migrations_upgrade_head_creates_schema(pg_engine) -> None:
    async with pg_engine.connect() as connection:
        tables = await connection.run_sync(lambda conn: inspect(conn).get_table_names())
    assert {"journal_entries", "entry_keys", "alembic_version"} <= set(tables)


async def test_models_match_migrations(pg_engine) -> None:
    async with pg_engine.begin() as connection:

        def check(conn):
            config = Config("alembic.ini")
            config.attributes["connection"] = conn
            command.check(config)

        await connection.run_sync(check)


async def test_downgrade_base_then_upgrade_head_roundtrip(pg_engine) -> None:
    async with pg_engine.begin() as connection:

        def roundtrip(conn):
            config = Config("alembic.ini")
            config.attributes["connection"] = conn
            command.downgrade(config, "base")
            command.upgrade(config, "head")
            command.upgrade(config, "head")

        await connection.run_sync(roundtrip)
    await test_migrations_upgrade_head_creates_schema(pg_engine)


async def test_pg_concurrent_authors_retry_without_overwriting_keys(
    pg_session_factory, journal_parts, mocker
) -> None:
    from envoy.journal.chain import JournalAuthor
    from envoy.journal.ports import JournalReader
    from envoy.journal.verifier import JournalVerifier

    session, agent, key, _, _, cipher, clock, _ = journal_parts
    vault = PgKeyVault(pg_session_factory, cipher.generate_key(), cipher)
    store = PgJournalStore(pg_session_factory, vault)
    reader = mocker.create_autospec(JournalReader, instance=True)
    barrier = asyncio.Event()
    arrived = 0

    async def head(identity):
        nonlocal arrived
        result = await store.head(identity)
        arrived += 1
        if arrived <= 2:
            if arrived == 2:
                barrier.set()
            await barrier.wait()
        return result

    reader.head.side_effect = head
    authors = [
        JournalAuthor(session, agent, key, store, reader, vault, cipher, clock) for _ in range(2)
    ]
    await asyncio.wait_for(
        asyncio.gather(
            *(
                author.record(JournalEntryType.decided, payload, b"c")
                for author, payload in zip(authors, [b"first", b"second"], strict=True)
            )
        ),
        timeout=5,
    )
    entries = await store.read(session)
    assert [entry.seq for entry in entries] == [0, 1]
    verifier = JournalVerifier()
    verifier.verify(entries, {agent: key.public()})
    assert {await verifier.read_payload(entry, vault, cipher) for entry in entries} == {
        b"first",
        b"second",
    }


async def test_pg_cancellation_rolls_back_entry_and_key(
    pg_engine, pg_session_factory, journal_parts
) -> None:
    from sqlalchemy import event

    from envoy.journal.chain import JournalAuthor

    session, agent, key, _, _, cipher, clock, _ = journal_parts
    vault = PgKeyVault(pg_session_factory, cipher.generate_key(), cipher)
    store = PgJournalStore(pg_session_factory, vault)
    author = JournalAuthor(session, agent, key, store, store, vault, cipher, clock)

    def cancel_key_insert(connection, cursor, statement, parameters, context, executemany):
        if statement.startswith("INSERT INTO entry_keys"):
            raise asyncio.CancelledError

    event.listen(pg_engine.sync_engine, "before_cursor_execute", cancel_key_insert)
    try:
        with pytest.raises(asyncio.CancelledError):
            await author.record(JournalEntryType.decided, b"data", b"c")
    finally:
        event.remove(pg_engine.sync_engine, "before_cursor_execute", cancel_key_insert)
    assert await store.read(session) == []
    with pytest.raises(ContentErased):
        await vault.entry_key(session, 0)
