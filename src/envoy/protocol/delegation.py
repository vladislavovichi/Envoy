import asyncio
from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import Any, Protocol
from uuid import UUID

from envoy.crypto.keys import PublicKey, SigningKey
from envoy.domain.canonical import encode
from envoy.domain.ids import AgentId, MandateId, OwnerId
from envoy.protocol.envelope import InvalidEnvelope

DELEGATION_CONTEXT = "envoy/delegation/v1"
ROTATION_CONTEXT = "envoy/rotate-key/v1"
REVOCATION_CONTEXT = "envoy/revoke-key/v1"
KEY_SIZE = 32
SIGNATURE_SIZE = 64


class DelegationError(Exception):
    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(f"Delegation rejected: {reason}")


@dataclass(frozen=True)
class Delegation:
    owner_id: OwnerId
    owner_key: bytes
    agent_id: AgentId
    bot_id: int
    agent_key: bytes
    mandate_id: MandateId
    version: int
    commitment: bytes
    expires_at: datetime
    signature: bytes = b""


def _delegation_fields(delegation: Delegation) -> dict[str, Any]:
    return {
        "owner_id": delegation.owner_id,
        "owner_key": delegation.owner_key,
        "agent_id": delegation.agent_id,
        "bot_id": delegation.bot_id,
        "agent_key": delegation.agent_key,
        "mandate_id": delegation.mandate_id,
        "version": delegation.version,
        "commitment": delegation.commitment,
        "expires_at": delegation.expires_at,
    }


def delegation_bytes(delegation: Delegation) -> bytes:
    return encode({"ctx": DELEGATION_CONTEXT, **_delegation_fields(delegation)})


def sign_delegation(delegation: Delegation, owner: SigningKey) -> Delegation:
    if owner.public().to_bytes() != delegation.owner_key:
        raise DelegationError("owner_key_mismatch")
    return replace(delegation, signature=owner.sign(delegation_bytes(delegation)))


def verify_delegation(delegation: Delegation, *, now: datetime, bot_id: int) -> None:
    if delegation.bot_id != bot_id:
        raise DelegationError("bot_mismatch")
    if now >= delegation.expires_at:
        raise DelegationError("delegation_expired")
    if not _key(delegation.owner_key).verify(delegation_bytes(delegation), delegation.signature):
        raise DelegationError("delegation_signature")


def delegation_map(delegation: Delegation) -> dict[str, Any]:
    return {**_delegation_fields(delegation), "signature": delegation.signature}


def _bytes(value: Any, size: int | None = None) -> bytes:
    if not isinstance(value, bytes) or (size is not None and len(value) != size):
        raise InvalidEnvelope("bad_chain")
    return value


def _uuid(value: Any) -> UUID:
    if not isinstance(value, UUID):
        raise InvalidEnvelope("bad_chain")
    return value


def _int(value: Any) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise InvalidEnvelope("bad_chain")
    return value


def delegation_from_map(raw: Mapping[str, Any] | None) -> Delegation:
    if raw is None:
        raise InvalidEnvelope("missing_chain")
    try:
        expires = raw["expires_at"]
        if not isinstance(expires, datetime) or expires.tzinfo is None:
            raise InvalidEnvelope("bad_chain")
        return Delegation(
            owner_id=OwnerId(_uuid(raw["owner_id"])),
            owner_key=_bytes(raw["owner_key"], KEY_SIZE),
            agent_id=AgentId(_uuid(raw["agent_id"])),
            bot_id=_int(raw["bot_id"]),
            agent_key=_bytes(raw["agent_key"], KEY_SIZE),
            mandate_id=MandateId(_uuid(raw["mandate_id"])),
            version=_int(raw["version"]),
            commitment=_bytes(raw["commitment"]),
            expires_at=expires.astimezone(UTC),
            signature=_bytes(raw["signature"], SIGNATURE_SIZE),
        )
    except KeyError:
        raise InvalidEnvelope("bad_chain") from None


def _key(raw: bytes) -> PublicKey:
    try:
        return PublicKey.from_bytes(raw)
    except ValueError:
        raise DelegationError("bad_key") from None


@dataclass(frozen=True)
class PeerPin:
    bot_id: int
    owner_id: OwnerId
    owner_key: bytes
    agent_id: AgentId
    agent_key: bytes
    revision: int = 0
    revoked: bool = False

    @classmethod
    def from_delegation(cls, delegation: Delegation) -> PeerPin:
        return cls(
            delegation.bot_id,
            delegation.owner_id,
            delegation.owner_key,
            delegation.agent_id,
            delegation.agent_key,
        )

    def same_keys(self, other: PeerPin) -> bool:
        return (self.owner_key, self.agent_key) == (other.owner_key, other.agent_key)


@dataclass(frozen=True)
class RotationProof:
    bot_id: int
    old_agent_key: bytes
    new_agent_key: bytes
    revision: int
    delegation: Delegation
    signature: bytes = b""


@dataclass(frozen=True)
class RevocationProof:
    bot_id: int
    agent_key: bytes
    revision: int
    reason: str
    signature: bytes = b""


def rotation_bytes(proof: RotationProof) -> bytes:
    return encode(
        {
            "ctx": ROTATION_CONTEXT,
            "bot_id": proof.bot_id,
            "old_agent_key": proof.old_agent_key,
            "new_agent_key": proof.new_agent_key,
            "revision": proof.revision,
            "delegation": delegation_bytes(proof.delegation),
        }
    )


def revocation_bytes(proof: RevocationProof) -> bytes:
    return encode(
        {
            "ctx": REVOCATION_CONTEXT,
            "bot_id": proof.bot_id,
            "agent_key": proof.agent_key,
            "revision": proof.revision,
            "reason": proof.reason,
        }
    )


def sign_rotation(proof: RotationProof, old_agent: SigningKey) -> RotationProof:
    return replace(proof, signature=old_agent.sign(rotation_bytes(proof)))


def sign_revocation(proof: RevocationProof, owner: SigningKey) -> RevocationProof:
    return replace(proof, signature=owner.sign(revocation_bytes(proof)))


def check_rotation(pin: PeerPin, proof: RotationProof) -> PeerPin:
    delegation = proof.delegation
    if pin.revoked:
        raise DelegationError("key_revoked")
    if proof.bot_id != pin.bot_id or delegation.bot_id != pin.bot_id:
        raise DelegationError("bot_mismatch")
    if proof.old_agent_key != pin.agent_key:
        raise DelegationError("old_key_mismatch")
    if proof.revision <= pin.revision:
        raise DelegationError("stale_revision")
    if (delegation.owner_id, delegation.owner_key) != (pin.owner_id, pin.owner_key):
        raise DelegationError("owner_mismatch")
    if delegation.agent_key != proof.new_agent_key:
        raise DelegationError("delegation_key_mismatch")
    if not _key(pin.agent_key).verify(rotation_bytes(proof), proof.signature):
        raise DelegationError("rotation_signature")
    if not _key(pin.owner_key).verify(delegation_bytes(delegation), delegation.signature):
        raise DelegationError("delegation_signature")
    return replace(
        pin, agent_id=delegation.agent_id, agent_key=proof.new_agent_key, revision=proof.revision
    )


def check_revocation(pin: PeerPin, proof: RevocationProof) -> PeerPin:
    if proof.bot_id != pin.bot_id:
        raise DelegationError("bot_mismatch")
    if proof.revision <= pin.revision:
        raise DelegationError("stale_revision")
    if proof.agent_key != pin.agent_key:
        raise DelegationError("old_key_mismatch")
    if not _key(pin.owner_key).verify(revocation_bytes(proof), proof.signature):
        raise DelegationError("revocation_signature")
    return replace(pin, revision=proof.revision, revoked=True)


class KeyRegistry(Protocol):
    async def get(self, bot_id: int) -> PeerPin | None: ...
    async def pin_first(self, pin: PeerPin) -> PeerPin: ...
    async def rotate(self, bot_id: int, proof: RotationProof) -> PeerPin: ...
    async def revoke(self, bot_id: int, proof: RevocationProof) -> None: ...
    async def history(self, bot_id: int) -> tuple[bytes, ...]: ...


class InMemoryKeyRegistry:
    def __init__(self) -> None:
        self._pins: dict[int, PeerPin] = {}
        self._history: dict[int, list[bytes]] = {}
        self._lock = asyncio.Lock()

    async def get(self, bot_id: int) -> PeerPin | None:
        return self._pins.get(bot_id)

    async def pin_first(self, pin: PeerPin) -> PeerPin:
        async with self._lock:
            existing = self._pins.get(pin.bot_id)
            if existing is not None:
                return existing
            self._pins[pin.bot_id] = pin
            self._history[pin.bot_id] = [pin.agent_key]
            return pin

    async def _current(self, bot_id: int) -> PeerPin:
        pin = self._pins.get(bot_id)
        if pin is None:
            raise DelegationError("not_pinned")
        return pin

    async def rotate(self, bot_id: int, proof: RotationProof) -> PeerPin:
        async with self._lock:
            pin = check_rotation(await self._current(bot_id), proof)
            self._pins[bot_id] = pin
            self._history[bot_id].append(pin.agent_key)
            return pin

    async def revoke(self, bot_id: int, proof: RevocationProof) -> None:
        async with self._lock:
            self._pins[bot_id] = check_revocation(await self._current(bot_id), proof)

    async def history(self, bot_id: int) -> tuple[bytes, ...]:
        return tuple(self._history.get(bot_id, ()))


def fingerprint_description(agent_key: PublicKey, text: str = "Envoy — ИИ-агент.") -> str:
    return f"{text} Ключ агента: {agent_key.fingerprint()}"
