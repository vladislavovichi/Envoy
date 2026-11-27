from dataclasses import dataclass, replace
from typing import Literal, Protocol

from envoy.domain.ids import MandateId, OwnerId
from envoy.mandate.schema import Mandate
from envoy.ports.ids import IdGenerator

type MandateStatus = Literal["draft", "active", "superseded", "discarded"]
type Attestation = Literal["platform", "owner"]


class MandateNotFound(LookupError):
    pass


class MandateStateError(ValueError):
    pass


@dataclass(frozen=True)
class StoredMandate:
    id: MandateId
    owner: OwnerId
    version: int
    mandate: Mandate
    salt: bytes
    status: MandateStatus
    signature: bytes | None = None
    attestation: Attestation = "platform"


class MandateStore(Protocol):
    async def save_draft(self, owner: OwnerId, mandate: Mandate, salt: bytes) -> StoredMandate: ...
    async def activate(self, mandate_id: MandateId, signature: bytes) -> StoredMandate: ...
    async def discard(self, mandate_id: MandateId) -> StoredMandate: ...
    async def get(self, mandate_id: MandateId) -> StoredMandate: ...
    async def active(self, owner: OwnerId) -> StoredMandate | None: ...
    async def activate_signed(
        self,
        owner: OwnerId,
        mandate_id: MandateId,
        expected_version: int | None,
        signature: bytes,
    ) -> StoredMandate: ...


def check_signed_activation(
    record: StoredMandate,
    current: StoredMandate | None,
    owner: OwnerId,
    expected_version: int | None,
    signature: bytes,
) -> bool:
    if record.owner != owner:
        raise MandateStateError("not_owner")
    if record.status == "active" and record.signature == signature:
        return False
    if record.status != "draft":
        raise MandateStateError(f"Mandate is {record.status}")
    if (None if current is None else current.version) != expected_version:
        raise MandateStateError("stale_version")
    return True


class InMemoryMandateStore:
    def __init__(self, ids: IdGenerator) -> None:
        self._ids = ids
        self._records: dict[MandateId, StoredMandate] = {}

    async def save_draft(self, owner: OwnerId, mandate: Mandate, salt: bytes) -> StoredMandate:
        version = 1 + max(
            (record.version for record in self._records.values() if record.owner == owner),
            default=0,
        )
        record = StoredMandate(
            MandateId(self._ids.new_id()),
            owner,
            version,
            mandate.model_copy(update={"version": version}),
            salt,
            "draft",
        )
        self._records[record.id] = record
        return record

    async def get(self, mandate_id: MandateId) -> StoredMandate:
        try:
            return self._records[mandate_id]
        except KeyError:
            raise MandateNotFound(str(mandate_id)) from None

    async def activate(self, mandate_id: MandateId, signature: bytes) -> StoredMandate:
        record = await self.get(mandate_id)
        if record.status != "draft":
            raise MandateStateError(f"Mandate is {record.status}")
        for other in list(self._records.values()):
            if other.owner == record.owner and other.status == "active":
                self._records[other.id] = replace(other, status="superseded")
        activated = replace(record, status="active", signature=signature)
        self._records[mandate_id] = activated
        return activated

    async def activate_signed(
        self,
        owner: OwnerId,
        mandate_id: MandateId,
        expected_version: int | None,
        signature: bytes,
    ) -> StoredMandate:
        record = await self.get(mandate_id)
        current = await self.active(owner)
        if not check_signed_activation(record, current, owner, expected_version, signature):
            return record
        if current is not None:
            self._records[current.id] = replace(current, status="superseded")
        activated = replace(record, status="active", signature=signature, attestation="owner")
        self._records[mandate_id] = activated
        return activated

    async def discard(self, mandate_id: MandateId) -> StoredMandate:
        record = await self.get(mandate_id)
        if record.status != "draft":
            raise MandateStateError(f"Mandate is {record.status}")
        discarded = replace(record, status="discarded")
        self._records[mandate_id] = discarded
        return discarded

    async def active(self, owner: OwnerId) -> StoredMandate | None:
        return next(
            (
                record
                for record in self._records.values()
                if record.owner == owner and record.status == "active"
            ),
            None,
        )
