from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime
from types import MappingProxyType

from envoy.crypto.keys import PublicKey
from envoy.protocol.bodies import CAPABILITY, CapabilitiesBody, HelloBody, parse_body
from envoy.protocol.delegation import (
    Delegation,
    DelegationError,
    KeyRegistry,
    PeerPin,
    delegation_from_map,
    verify_delegation,
)
from envoy.protocol.envelope import (
    WIRE_VERSION,
    DecodedEnvelope,
    InvalidEnvelope,
    Version,
    unknown_critical,
)

SUPPORTED_VERSIONS = (WIRE_VERSION, WIRE_VERSION)
UNDERSTOOD_CRITICAL: frozenset[str] = frozenset()


@dataclass(frozen=True)
class Capabilities:
    versions: Mapping[str, frozenset[int]]

    @classmethod
    def parse(cls, items: Iterable[str]) -> Capabilities:
        versions: dict[str, set[int]] = {}
        for item in items:
            match = CAPABILITY.fullmatch(item)
            if match is None:
                raise InvalidEnvelope("bad_body")
            versions.setdefault(match.group(1), set()).add(int(match.group(2)))
        return cls(MappingProxyType({name: frozenset(found) for name, found in versions.items()}))

    def items(self) -> tuple[str, ...]:
        return tuple(
            f"{name}@{version}"
            for name in sorted(self.versions)
            for version in sorted(self.versions[name])
        )


@dataclass(frozen=True)
class NegotiatedCapabilities:
    chosen: Mapping[str, int]

    def items(self) -> tuple[str, ...]:
        return tuple(f"{name}@{version}" for name, version in sorted(self.chosen.items()))


def negotiate(local: Capabilities, remote: Capabilities) -> NegotiatedCapabilities:
    chosen = {
        name: max(local.versions[name] & remote.versions[name])
        for name in local.versions.keys() & remote.versions.keys()
        if local.versions[name] & remote.versions[name]
    }
    return NegotiatedCapabilities(MappingProxyType(chosen))


def negotiate_version(
    local: tuple[Version, Version], remote: tuple[Version, Version]
) -> Version | None:
    low = max(local[0], remote[0])
    high = min(local[1], remote[1])
    if low > high or low.major != high.major:
        return None
    return high


def supported(versions: tuple[Version, Version] = SUPPORTED_VERSIONS) -> tuple[str, ...]:
    return tuple(sorted({str(versions[0]), str(versions[1])}))


@dataclass(frozen=True)
class Accepted:
    pin: PeerPin
    delegation: Delegation
    hello: HelloBody
    version: Version


@dataclass(frozen=True)
class Rejected:
    reason: str
    authenticated: bool
    supported: tuple[str, ...] = ()


@dataclass(frozen=True)
class KeyChanged:
    pinned: PeerPin
    presented: PeerPin


type HelloResult = Accepted | Rejected | KeyChanged


async def verify_hello(
    message: DecodedEnvelope,
    *,
    from_bot_id: int,
    now: datetime,
    registry: KeyRegistry,
    versions: tuple[Version, Version] = SUPPORTED_VERSIONS,
) -> HelloResult:
    envelope = message.envelope
    if envelope.type != "hello":
        return Rejected("not_hello", False)
    try:
        delegation = delegation_from_map(envelope.chain)
        verify_delegation(delegation, now=now, bot_id=from_bot_id)
        agent_key = PublicKey.from_bytes(delegation.agent_key)
    except InvalidEnvelope as error:
        return Rejected(error.reason, False)
    except DelegationError as error:
        return Rejected(error.reason, False)
    except ValueError:
        return Rejected("bad_key", False)
    if not message.verify(agent_key):
        return Rejected("bad_signature", False)
    if unknown_critical(envelope, UNDERSTOOD_CRITICAL):
        return Rejected("unsupported", True, supported(versions))
    try:
        hello = parse_body("hello", envelope.body)
    except InvalidEnvelope:
        return Rejected("bad_body", True)
    assert isinstance(hello, HelloBody)
    if (hello.agent_id, hello.owner_id) != (delegation.agent_id.bytes, delegation.owner_id.bytes):
        return Rejected("identity_mismatch", True)
    remote = (hello.min_version.version(), hello.max_version.version())
    version = negotiate_version(versions, remote)
    if version is None or envelope.v.major != version.major:
        return Rejected("unsupported", True, supported(versions))
    presented = PeerPin.from_delegation(delegation)
    pinned = await registry.get(from_bot_id)
    if pinned is None:
        pinned = await registry.pin_first(presented)
    if pinned.revoked:
        return Rejected("key_revoked", True)
    if not pinned.same_keys(presented):
        return KeyChanged(pinned, presented)
    return Accepted(pinned, delegation, hello, version)


def capabilities_of(body: CapabilitiesBody) -> Capabilities:
    return Capabilities.parse(body.items)
