import re
from collections.abc import Mapping
from typing import Any, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from envoy.domain.ids import OfferHash
from envoy.domain.offer import IssueValue, Offer
from envoy.protocol.envelope import InvalidEnvelope, MessageType, Version

HASH_SIZE = 32
CAPABILITY = re.compile(r"([a-z][a-z0-9_-]*(?::[a-z0-9_.-]+)*)@([1-9][0-9]{0,3})")

type Stage = Literal["ready", "decision"]
type Decision = Literal["commit", "abort"]


class _Body(BaseModel):
    model_config = ConfigDict(frozen=True, extra="ignore", strict=True)


class WireVersion(_Body):
    major: int = Field(ge=0)
    minor: int = Field(ge=0)

    def version(self) -> Version:
        return Version(self.major, self.minor)


class HelloBody(_Body):
    agent_id: bytes = Field(min_length=16, max_length=16)
    owner_id: bytes = Field(min_length=16, max_length=16)
    min_version: WireVersion
    max_version: WireVersion
    token: str | None = Field(default=None, max_length=128)

    @model_validator(mode="after")
    def ordered_versions(self) -> Self:
        if self.min_version.version() > self.max_version.version():
            raise ValueError("min_version must not exceed max_version")
        return self


class CapabilitiesBody(_Body):
    items: tuple[str, ...] = Field(max_length=32)

    @model_validator(mode="after")
    def well_formed(self) -> Self:
        if not all(CAPABILITY.fullmatch(item) for item in self.items):
            raise ValueError("capability must look like name@version")
        return self


class OfferBody(_Body):
    offer: Mapping[str, IssueValue]
    offer_hash: bytes = Field(min_length=HASH_SIZE, max_length=HASH_SIZE)
    previous: bytes | None = Field(default=None, min_length=HASH_SIZE, max_length=HASH_SIZE)
    round: int = Field(ge=0)

    @model_validator(mode="after")
    def hash_matches(self) -> Self:
        if self.public_offer().hash() != self.offer_hash:
            raise ValueError("offer_hash does not match offer")
        return self

    def public_offer(self) -> Offer:
        return Offer(values=dict(self.offer))


class ProposeBody(OfferBody):
    pass


class CounterBody(OfferBody):
    pass


class AcceptBody(_Body):
    offer_hash: bytes = Field(min_length=HASH_SIZE, max_length=HASH_SIZE)


class RejectBody(_Body):
    reason: str = Field(max_length=64)
    supported: tuple[str, ...] = Field(default=(), max_length=32)


class CommitBody(_Body):
    stage: Stage
    offer_hash: bytes = Field(min_length=HASH_SIZE, max_length=HASH_SIZE)
    decision: Decision | None = None

    @model_validator(mode="after")
    def decision_only_in_decision_stage(self) -> Self:
        if (self.stage == "decision") != (self.decision is not None):
            raise ValueError("decision is required exactly in the decision stage")
        return self


class RevokeBody(_Body):
    offer_hash: bytes = Field(min_length=HASH_SIZE, max_length=HASH_SIZE)


class DisputeBody(_Body):
    entries: tuple[bytes, ...] = Field(min_length=1, max_length=32)
    commitment_reveal: bytes | None = Field(default=None, max_length=256)


class RotateKeyBody(_Body):
    proof: Mapping[str, Any]


class RevokeKeyBody(_Body):
    proof: Mapping[str, Any]


class DecisionRequestBody(_Body):
    offer_hash: bytes = Field(min_length=HASH_SIZE, max_length=HASH_SIZE)
    cursor: int = Field(ge=0)


class DecisionBody(_Body):
    offer_hash: bytes = Field(min_length=HASH_SIZE, max_length=HASH_SIZE)
    decision: Decision
    cursor: int = Field(ge=0)
    decided_at: int = Field(ge=0)


class AckBody(_Body):
    pass


class PingBody(_Body):
    pass


class ErrorBody(_Body):
    code: str = Field(max_length=64)


type Body = (
    HelloBody
    | CapabilitiesBody
    | ProposeBody
    | CounterBody
    | AcceptBody
    | RejectBody
    | CommitBody
    | RevokeBody
    | DisputeBody
    | RotateKeyBody
    | RevokeKeyBody
    | DecisionRequestBody
    | DecisionBody
    | AckBody
    | PingBody
    | ErrorBody
)

BODIES: Mapping[MessageType, type[_Body]] = {
    "hello": HelloBody,
    "capabilities": CapabilitiesBody,
    "propose": ProposeBody,
    "counter": CounterBody,
    "accept": AcceptBody,
    "reject": RejectBody,
    "commit": CommitBody,
    "revoke": RevokeBody,
    "dispute": DisputeBody,
    "rotate_key": RotateKeyBody,
    "revoke_key": RevokeKeyBody,
    "decision_request": DecisionRequestBody,
    "decision": DecisionBody,
    "ack": AckBody,
    "ping": PingBody,
    "error": ErrorBody,
}


def parse_body(kind: MessageType, body: Mapping[str, Any]) -> Body:
    values = {key: tuple(item) if isinstance(item, list) else item for key, item in body.items()}
    try:
        return BODIES[kind].model_validate(values)  # type: ignore[return-value]
    except ValidationError:
        raise InvalidEnvelope("bad_body") from None


def offer_body(offer: Offer, round: int, previous: OfferHash | None = None) -> dict[str, Any]:
    return {
        "offer": dict(offer.values),
        "offer_hash": offer.hash(),
        "previous": previous,
        "round": round,
    }


def version_map(version: Version) -> dict[str, int]:
    return {"major": version.major, "minor": version.minor}
