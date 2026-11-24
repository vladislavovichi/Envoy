import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from envoy.domain.errors import ChainBroken, SignatureInvalid
from envoy.domain.events import JournalEntryType
from envoy.journal.verifier import JournalVerifier


@settings(max_examples=25, suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(
    st.lists(st.binary(max_size=128), min_size=1, max_size=20),
    st.integers(min_value=0, max_value=999),
    st.sampled_from(["payload_ciphertext", "entry_hash", "signature", "mandate_commitment"]),
)
async def test_any_single_byte_mutation_of_any_entry_is_detected(
    journal_parts, payloads, index, field
) -> None:
    session, agent, key, store, _, _, _, author = journal_parts
    for payload in payloads:
        await author.record(JournalEntryType.decided, payload, b"commitment")
    entries = await store.read(session)
    target = index % len(entries)
    changed = bytearray(getattr(entries[target], field))
    changed[index % len(changed)] ^= 1
    entries[target] = entries[target].model_copy(update={field: bytes(changed)})
    with pytest.raises((ChainBroken, SignatureInvalid)):
        JournalVerifier().verify(entries, {agent: key.public()})
