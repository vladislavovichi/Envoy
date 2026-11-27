import asyncio

import pytest

from envoy.crypto.keys import SigningKey
from envoy.persistence.key_registry import PgKeyRegistry
from envoy.protocol.delegation import (
    DelegationError,
    InMemoryKeyRegistry,
    PeerPin,
    RevocationProof,
    RotationProof,
    sign_revocation,
    sign_rotation,
)
from tests.support.agents import ALICE

NEW_AGENT = SigningKey.from_bytes(bytes([42]) * 32)


@pytest.fixture(params=["memory", "postgres"])
def backend(request):
    return request.param


@pytest.fixture
def factory(backend, request):
    return None if backend == "memory" else request.getfixturevalue("pg_session_factory")


def registry_for(backend, factory, shared=None):
    if backend == "memory":
        return shared or InMemoryKeyRegistry()
    return PgKeyRegistry(factory)


@pytest.fixture
def registry(backend, factory):
    return registry_for(backend, factory)


def pin(agent: SigningKey = ALICE.agent) -> PeerPin:
    return PeerPin.from_delegation(ALICE.delegation(agent=agent))


def rotation(revision: int = 1, *, old: SigningKey = ALICE.agent, owner=None) -> RotationProof:
    delegation = ALICE.delegation(agent=NEW_AGENT, owner=owner)
    proof = RotationProof(
        ALICE.bot_id,
        ALICE.agent.public().to_bytes(),
        NEW_AGENT.public().to_bytes(),
        revision,
        delegation,
    )
    return sign_rotation(proof, old)


def revocation(revision: int, key: SigningKey = ALICE.agent, owner=None) -> RevocationProof:
    proof = RevocationProof(ALICE.bot_id, key.public().to_bytes(), revision, "compromised")
    return sign_revocation(proof, owner or ALICE.owner)


async def test_concurrent_first_keys_produce_one_pin(registry) -> None:
    first, second = pin(), pin(SigningKey.generate())
    results = await asyncio.gather(registry.pin_first(first), registry.pin_first(second))
    assert results[0] == results[1]
    stored = await registry.get(ALICE.bot_id)
    assert stored == results[0]
    assert await registry.history(ALICE.bot_id) == (stored.agent_key,)


async def test_rotation_requires_old_key_signature_and_owner_delegation(registry) -> None:
    await registry.pin_first(pin())
    for forged, reason in (
        (rotation(old=SigningKey.generate()), "rotation_signature"),
        (rotation(owner=SigningKey.generate()), "owner_mismatch"),
        (rotation(0), "stale_revision"),
    ):
        with pytest.raises(DelegationError, match=reason):
            await registry.rotate(ALICE.bot_id, forged)
    assert (await registry.get(ALICE.bot_id)).agent_key == ALICE.agent.public().to_bytes()
    rotated = await registry.rotate(ALICE.bot_id, rotation(1))
    assert rotated.agent_key == NEW_AGENT.public().to_bytes()
    assert rotated.revision == 1
    with pytest.raises(DelegationError):
        await registry.rotate(ALICE.bot_id, rotation(1))
    assert await registry.history(ALICE.bot_id) == (
        ALICE.agent.public().to_bytes(),
        NEW_AGENT.public().to_bytes(),
    )


async def test_owner_revocation_survives_restart_and_rejects_stale_proof(backend, factory) -> None:
    shared = InMemoryKeyRegistry()
    registry = registry_for(backend, factory, shared)
    await registry.pin_first(pin())
    with pytest.raises(DelegationError, match="revocation_signature"):
        await registry.revoke(ALICE.bot_id, revocation(1, owner=SigningKey.generate()))
    await registry.revoke(ALICE.bot_id, revocation(1))
    restarted = registry_for(backend, factory, shared)
    stored = await restarted.get(ALICE.bot_id)
    assert stored.revoked
    assert stored.revision == 1
    with pytest.raises(DelegationError):
        await restarted.revoke(ALICE.bot_id, revocation(1))
    with pytest.raises(DelegationError, match="key_revoked"):
        await restarted.rotate(ALICE.bot_id, rotation(2))


async def test_unknown_bot_cannot_be_rotated_or_revoked(registry) -> None:
    with pytest.raises(DelegationError, match="not_pinned"):
        await registry.rotate(ALICE.bot_id, rotation())
    with pytest.raises(DelegationError, match="not_pinned"):
        await registry.revoke(ALICE.bot_id, revocation(1))
    assert await registry.history(ALICE.bot_id) == ()
