import asyncio

import pytest

from envoy.crypto.keys import SigningKey
from envoy.domain.errors import ChainBroken, ContentErased, SequenceConflict, SignatureInvalid
from envoy.domain.events import JournalEntryType
from envoy.journal.chain import JournalAuthor
from envoy.journal.verifier import JournalVerifier


def test_empty_journal_is_valid() -> None:
    JournalVerifier().verify([], {})


@pytest.mark.parametrize("payload", [b"", b"hello", bytes(1_048_576)])
async def test_single_entry_verifies(journal_parts, payload: bytes) -> None:
    _, agent, key, _, vault, cipher, _, author = journal_parts
    entry = await author.record(JournalEntryType.session_opened, payload, b"commitment")
    JournalVerifier().verify([entry], {agent: key.public()})
    assert await JournalVerifier().read_payload(entry, vault, cipher) == payload


async def test_three_entries_chain_verifies(journal_parts) -> None:
    session, agent, key, store, _, _, _, author = journal_parts
    for _ in range(3):
        await author.record(JournalEntryType.decided, b"payload", b"commitment")
    entries = await store.read(session)
    assert [entry.seq for entry in entries] == [0, 1, 2]
    JournalVerifier().verify(entries, {agent: key.public()})


@pytest.mark.parametrize(
    "field",
    [
        "payload_ciphertext",
        "prev_hash",
        "mandate_commitment",
        "entry_hash",
        "ts",
        "author",
        "session_id",
        "type",
    ],
)
async def test_modified_entry_detected(journal_parts, field: str) -> None:
    from datetime import timedelta
    from uuid import UUID

    _, agent, key, _, _, _, _, author = journal_parts
    entry = await author.record(JournalEntryType.session_opened, b"data", b"commitment")
    replacement = b"changed"
    if field == "ts":
        replacement = entry.ts + timedelta(seconds=1)
    if field in {"author", "session_id"}:
        replacement = UUID(int=999)
    if field == "type":
        replacement = JournalEntryType.closed
    changed = entry.model_copy(update={field: replacement})
    with pytest.raises((ChainBroken, SignatureInvalid)):
        JournalVerifier().verify([changed], {agent: key.public()})


@pytest.mark.parametrize("mutation", ["reorder", "remove", "foreign", "unknown_author"])
async def test_chain_or_signature_tampering_detected(journal_parts, mutation: str) -> None:
    session, agent, key, store, _, _, _, author = journal_parts
    for _ in range(3):
        await author.record(JournalEntryType.decided, b"data", b"commitment")
    entries = await store.read(session)
    if mutation == "reorder":
        entries.reverse()
    if mutation == "remove":
        del entries[1]
    if mutation == "foreign":
        entries[0] = entries[0].model_copy(update={"signature": SigningKey.generate().sign(b"bad")})
    with pytest.raises((ChainBroken, SignatureInvalid)):
        JournalVerifier().verify(
            entries, {} if mutation == "unknown_author" else {agent: key.public()}
        )


async def test_erased_key_keeps_chain_valid_and_read_raises_content_erased(journal_parts) -> None:
    session, agent, key, _, vault, cipher, _, author = journal_parts
    entry = await author.record(JournalEntryType.session_opened, b"secret", b"commitment")
    await vault.erase(session, entry.seq)
    await vault.erase(session, entry.seq)
    JournalVerifier().verify([entry], {agent: key.public()})
    with pytest.raises(ContentErased):
        await JournalVerifier().read_payload(entry, vault, cipher)


async def test_concurrent_appends_produce_gapless_seq(journal_parts) -> None:
    session, agent, key, store, vault, cipher, clock, author = journal_parts
    other = JournalAuthor(session, agent, key, store, store, vault, cipher, clock)
    await asyncio.gather(
        author.record(JournalEntryType.decided, b"one", b"c"),
        other.record(JournalEntryType.decided, b"two", b"c"),
    )
    entries = await store.read(session)
    assert [entry.seq for entry in entries] == [0, 1]
    assert {await JournalVerifier().read_payload(entry, vault, cipher) for entry in entries} == {
        b"one",
        b"two",
    }
    with pytest.raises(SequenceConflict):
        await store.append(entries[0])


async def test_author_retries_conflict_then_gives_up(journal_parts, mocker) -> None:
    from envoy.journal.ports import JournalCommitter

    session, agent, key, store, vault, cipher, clock, _ = journal_parts
    writer = mocker.create_autospec(JournalCommitter, instance=True)
    writer.commit.side_effect = SequenceConflict(0)
    author = JournalAuthor(session, agent, key, writer, store, vault, cipher, clock)
    with pytest.raises(SequenceConflict):
        await author.record(JournalEntryType.decided, b"one", b"c")
    assert writer.commit.await_count == 4


async def test_key_failure_does_not_publish_journal_entry(journal_parts) -> None:
    session, _, _, store, vault, _, _, author = journal_parts
    await vault.put(session, 0, b"reserved-key")
    with pytest.raises(SequenceConflict):
        await author.record(JournalEntryType.decided, b"must-not-be-published", b"c")
    assert await store.read(session) == []
    assert await vault.entry_key(session, 0) == b"reserved-key"
