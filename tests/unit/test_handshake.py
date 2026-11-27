from datetime import timedelta

import pytest

from envoy.crypto.keys import SigningKey
from envoy.protocol.delegation import (
    DelegationError,
    InMemoryKeyRegistry,
    delegation_bytes,
    delegation_from_map,
    delegation_map,
    verify_delegation,
)
from envoy.protocol.envelope import InvalidEnvelope, Version
from envoy.protocol.handshake import (
    Accepted,
    Capabilities,
    KeyChanged,
    Rejected,
    negotiate,
    negotiate_version,
    verify_hello,
)
from tests.support.agents import ALICE, BOB
from tests.support.sessions import T0


async def handshake(message, *, from_bot_id=ALICE.bot_id, now=T0, registry=None):
    return await verify_hello(
        message, from_bot_id=from_bot_id, now=now, registry=registry or InMemoryKeyRegistry()
    )


def test_capability_intersection_picks_highest_common_version() -> None:
    local = Capabilities.parse(["issue:datetime@1", "issue:datetime@2", "deposit:stars@1"])
    remote = Capabilities.parse(["issue:datetime@1", "psi:dh-ristretto255@1"])
    assert negotiate(local, remote).items() == ("issue:datetime@1",)
    both = Capabilities.parse(["issue:datetime@1", "issue:datetime@2"])
    assert negotiate(local, both).chosen == {"issue:datetime": 2}
    assert negotiate(local, Capabilities.parse([])).items() == ()
    with pytest.raises(InvalidEnvelope):
        Capabilities.parse(["datetime"])


def test_version_negotiation() -> None:
    one = (Version(1, 0), Version(1, 3))
    assert negotiate_version(one, (Version(1, 1), Version(1, 5))) == Version(1, 3)
    assert negotiate_version(one, (Version(2, 0), Version(2, 1))) is None
    assert negotiate_version(one, (Version(1, 4), Version(1, 9))) is None
    assert negotiate_version((Version(1, 0), Version(2, 0)), (Version(1, 5), Version(2, 0))) is None


def test_delegation_binds_bot_expiry_and_owner_signature() -> None:
    delegation = ALICE.delegation(expires_at=T0 + timedelta(seconds=1))
    verify_delegation(delegation, now=T0, bot_id=ALICE.bot_id)
    with pytest.raises(DelegationError, match="delegation_expired"):
        verify_delegation(delegation, now=T0 + timedelta(seconds=1), bot_id=ALICE.bot_id)
    with pytest.raises(DelegationError, match="bot_mismatch"):
        verify_delegation(delegation, now=T0, bot_id=BOB.bot_id)
    forged = delegation_from_map({**delegation_map(delegation), "version": 2})
    with pytest.raises(DelegationError, match="delegation_signature"):
        verify_delegation(forged, now=T0, bot_id=ALICE.bot_id)
    assert delegation_from_map(delegation_map(delegation)) == delegation
    assert delegation_bytes(delegation).startswith(b"\xaa")
    with pytest.raises(InvalidEnvelope):
        delegation_from_map({"owner_id": 1})
    with pytest.raises(InvalidEnvelope):
        delegation_from_map(None)


async def test_valid_hello_pins_keys_on_first_contact() -> None:
    registry = InMemoryKeyRegistry()
    result = await handshake(ALICE.hello(), registry=registry)
    assert isinstance(result, Accepted)
    assert result.version == Version(1, 0)
    pin = await registry.get(ALICE.bot_id)
    assert pin.agent_key == ALICE.agent.public().to_bytes()
    assert pin.owner_key == ALICE.owner.public().to_bytes()
    assert isinstance(await handshake(ALICE.hello(), registry=registry), Accepted)


async def test_expired_delegation_at_exact_boundary_is_rejected() -> None:
    message = ALICE.hello(delegation=ALICE.delegation(expires_at=T0))
    result = await handshake(message)
    assert result == Rejected("delegation_expired", False)


async def test_foreign_telegram_sender_is_rejected_and_not_pinned() -> None:
    registry = InMemoryKeyRegistry()
    result = await handshake(ALICE.hello(), from_bot_id=BOB.bot_id, registry=registry)
    assert result == Rejected("bot_mismatch", False)
    assert await registry.get(BOB.bot_id) is None
    assert await registry.get(ALICE.bot_id) is None


async def test_hello_signed_by_other_key_is_unauthenticated_rejection() -> None:
    result = await handshake(ALICE.hello(signer=SigningKey.generate()))
    assert result == Rejected("bad_signature", False)


async def test_key_change_without_proof_escalates_and_keeps_pin() -> None:
    registry = InMemoryKeyRegistry()
    await handshake(ALICE.hello(), registry=registry)
    stranger = SigningKey.generate()
    changed = ALICE.hello(delegation=ALICE.delegation(agent=stranger), signer=stranger)
    result = await handshake(changed, registry=registry)
    assert isinstance(result, KeyChanged)
    assert result.presented.agent_key == stranger.public().to_bytes()
    assert (await registry.get(ALICE.bot_id)).agent_key == ALICE.agent.public().to_bytes()
    other_owner = SigningKey.generate()
    hijack = ALICE.hello(delegation=ALICE.delegation(owner=other_owner))
    assert isinstance(await handshake(hijack, registry=registry), KeyChanged)


@pytest.mark.parametrize(
    "options",
    [
        {"major": 2, "versions": (Version(2, 0), Version(2, 0))},
        {"versions": (Version(1, 1), Version(1, 4))},
        {"crit": ("x-psi",)},
    ],
)
async def test_incompatible_version_or_unknown_critical_field_is_unsupported(options) -> None:
    registry = InMemoryKeyRegistry()
    result = await handshake(ALICE.hello(**options), registry=registry)
    assert result == Rejected("unsupported", True, ("1.0",))
    assert await registry.get(ALICE.bot_id) is None


async def test_revoked_pin_rejects_hello() -> None:
    from envoy.protocol.delegation import RevocationProof, sign_revocation

    registry = InMemoryKeyRegistry()
    await handshake(ALICE.hello(), registry=registry)
    proof = RevocationProof(ALICE.bot_id, ALICE.agent.public().to_bytes(), 1, "lost")
    await registry.revoke(ALICE.bot_id, sign_revocation(proof, ALICE.owner))
    assert await handshake(ALICE.hello(), registry=registry) == Rejected("key_revoked", True)


async def test_not_hello_and_missing_chain_are_rejected() -> None:
    from envoy.protocol.envelope import Envelope, decode_message, encode_message
    from tests.support.agents import SID

    ping = decode_message(
        encode_message(
            Envelope(
                v=Version(1, 0),
                sid=SID,
                seq=1,
                ack=0,
                type="hello",
                body={},
                ts=0,
                exp=1,
                nonce=bytes(16),
            ),
            ALICE.agent,
        )
    )
    assert await handshake(ping) == Rejected("missing_chain", False)


def test_delegation_and_proof_validation_branches() -> None:
    from dataclasses import replace as change
    from datetime import datetime

    from envoy.protocol.delegation import (
        PeerPin,
        RevocationProof,
        RotationProof,
        check_revocation,
        check_rotation,
        sign_delegation,
        sign_revocation,
        sign_rotation,
    )

    delegation = ALICE.delegation()
    with pytest.raises(DelegationError, match="owner_key_mismatch"):
        sign_delegation(delegation, BOB.owner)
    raw = delegation_map(delegation)
    for field, value in (
        ("owner_id", "x"),
        ("owner_key", b"short"),
        ("bot_id", -1),
        ("bot_id", True),
        ("expires_at", datetime(2026, 1, 1)),
    ):
        with pytest.raises(InvalidEnvelope):
            delegation_from_map({**raw, field: value})
    with pytest.raises(DelegationError, match="bad_key"):
        verify_delegation(
            change(delegation, owner_key=b"\x01" * 31),
            now=T0,
            bot_id=ALICE.bot_id,
        )
    pin = PeerPin.from_delegation(delegation)
    new = SigningKey.generate()
    good = RotationProof(
        ALICE.bot_id,
        ALICE.agent.public().to_bytes(),
        new.public().to_bytes(),
        1,
        ALICE.delegation(agent=new),
    )
    for proof, reason in (
        (change(good, bot_id=BOB.bot_id), "bot_mismatch"),
        (change(good, old_agent_key=new.public().to_bytes()), "old_key_mismatch"),
        (change(good, new_agent_key=bytes(32)), "delegation_key_mismatch"),
    ):
        with pytest.raises(DelegationError, match=reason):
            check_rotation(pin, sign_rotation(proof, ALICE.agent))
    forged = sign_rotation(good, ALICE.agent)
    forged = change(forged, delegation=change(forged.delegation, signature=bytes(64)))
    with pytest.raises(DelegationError, match="delegation_signature"):
        check_rotation(pin, forged)
    revocation = RevocationProof(ALICE.bot_id, ALICE.agent.public().to_bytes(), 1, "lost")
    for proof, reason in (
        (change(revocation, bot_id=BOB.bot_id), "bot_mismatch"),
        (change(revocation, agent_key=new.public().to_bytes()), "old_key_mismatch"),
    ):
        with pytest.raises(DelegationError, match=reason):
            check_revocation(pin, sign_revocation(proof, ALICE.owner))
