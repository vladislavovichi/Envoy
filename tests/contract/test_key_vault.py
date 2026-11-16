from uuid import UUID

import pytest

from envoy.crypto.aead import ContentCipher
from envoy.domain.errors import ContentErased, SequenceConflict
from envoy.domain.ids import SessionId
from envoy.journal.memory import InMemoryKeyVault
from envoy.persistence.key_vault import PgKeyVault
from envoy.ports.rng import SeededRng


@pytest.fixture(params=["memory", "postgres"])
def vault(request):
    if request.param == "memory":
        return InMemoryKeyVault()
    sessions = request.getfixturevalue("pg_session_factory")
    cipher = ContentCipher(SeededRng(5))
    return PgKeyVault(sessions, cipher.generate_key(), cipher)


async def test_key_vault_isolates_sessions_and_rejects_overwrites(vault) -> None:
    first, second = SessionId(UUID(int=1)), SessionId(UUID(int=2))
    await vault.put(first, 0, b"one")
    await vault.put(second, 0, b"two")
    with pytest.raises(SequenceConflict):
        await vault.put(first, 0, b"replacement")
    assert await vault.entry_key(first, 0) == b"one"
    await vault.erase(first, 0)
    with pytest.raises(ContentErased):
        await vault.entry_key(first, 0)
    assert await vault.entry_key(second, 0) == b"two"
