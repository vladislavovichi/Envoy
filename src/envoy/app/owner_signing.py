import asyncio
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from datetime import datetime
from types import MappingProxyType
from typing import Any, Protocol

from envoy.domain.ids import MandateId, OwnerId
from envoy.mandate.ports import MandateNotFound, MandateStateError, MandateStore
from envoy.mandate.signatures import Purpose
from envoy.protocol.delegation import Delegation


class SigningRejected(PermissionError):
    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(f"Signing rejected: {reason}")


@dataclass(frozen=True)
class StoredChallenge:
    id: str
    owner: OwnerId
    purpose: Purpose
    nonce: bytes
    bytes_to_sign: bytes
    expires_at: datetime
    context: Mapping[str, Any] = field(default_factory=lambda: MappingProxyType({}))
    signature: bytes | None = None
    result: Mapping[str, Any] | None = None


@dataclass(frozen=True)
class EnrollKey:
    owner: OwnerId
    public_key: bytes


@dataclass(frozen=True)
class ActivateMandate:
    owner: OwnerId
    mandate_id: MandateId
    expected_version: int | None
    signature: bytes


@dataclass(frozen=True)
class StoreDelegation:
    delegation: Delegation


@dataclass(frozen=True)
class RevokeDelegations:
    owner: OwnerId
    agent_key: bytes


@dataclass(frozen=True)
class NoMutation:
    pass


type Mutation = EnrollKey | ActivateMandate | StoreDelegation | RevokeDelegations | NoMutation


class OwnerSigningStore(Protocol):
    async def owner_key(self, owner: OwnerId) -> bytes | None: ...
    async def put_challenge(self, challenge: StoredChallenge) -> None: ...
    async def get_challenge(self, id: str) -> StoredChallenge | None: ...
    async def redeem(
        self,
        id: str,
        signature: bytes,
        mutation: Mutation,
        result: Mapping[str, Any],
        now: datetime,
    ) -> StoredChallenge: ...
    async def current(self, owner: OwnerId) -> Delegation | None: ...


def check_redeemable(challenge: StoredChallenge | None, signature: bytes, now: datetime) -> bool:
    if challenge is None:
        raise SigningRejected("unknown_challenge")
    if challenge.signature is not None:
        if challenge.signature == signature:
            return False
        raise SigningRejected("challenge_used")
    if now >= challenge.expires_at:
        raise SigningRejected("challenge_expired")
    return True


class InMemoryOwnerSigningStore:
    def __init__(self, mandates: MandateStore) -> None:
        self._mandates = mandates
        self._keys: dict[OwnerId, bytes] = {}
        self._challenges: dict[str, StoredChallenge] = {}
        self._delegations: dict[OwnerId, list[tuple[Delegation, bool]]] = {}
        self._lock = asyncio.Lock()

    async def owner_key(self, owner: OwnerId) -> bytes | None:
        return self._keys.get(owner)

    async def put_challenge(self, challenge: StoredChallenge) -> None:
        if challenge.id in self._challenges:
            raise SigningRejected("duplicate_challenge")
        self._challenges[challenge.id] = challenge

    async def get_challenge(self, id: str) -> StoredChallenge | None:
        return self._challenges.get(id)

    async def _apply(self, mutation: Mutation) -> None:
        match mutation:
            case EnrollKey(owner=owner, public_key=key):
                existing = self._keys.get(owner)
                if existing is not None and existing != key:
                    raise SigningRejected("already_enrolled")
                self._keys[owner] = key
            case ActivateMandate():
                try:
                    await self._mandates.activate_signed(
                        mutation.owner,
                        mutation.mandate_id,
                        mutation.expected_version,
                        mutation.signature,
                    )
                except (MandateStateError, MandateNotFound) as error:
                    raise SigningRejected(str(error)) from None
            case StoreDelegation(delegation=delegation):
                self._delegations.setdefault(delegation.owner_id, []).append((delegation, False))
            case RevokeDelegations(owner=owner, agent_key=key):
                self._delegations[owner] = [
                    (delegation, revoked or delegation.agent_key == key)
                    for delegation, revoked in self._delegations.get(owner, [])
                ]
            case NoMutation():
                pass

    async def redeem(
        self,
        id: str,
        signature: bytes,
        mutation: Mutation,
        result: Mapping[str, Any],
        now: datetime,
    ) -> StoredChallenge:
        async with self._lock:
            challenge = self._challenges.get(id)
            if not check_redeemable(challenge, signature, now):
                assert challenge is not None
                return challenge
            assert challenge is not None
            await self._apply(mutation)
            redeemed = replace(
                challenge, signature=signature, result=MappingProxyType(dict(result))
            )
            self._challenges[id] = redeemed
            return redeemed

    async def current(self, owner: OwnerId) -> Delegation | None:
        active = [
            delegation for delegation, revoked in self._delegations.get(owner, []) if not revoked
        ]
        return active[-1] if active else None
