from collections.abc import Mapping
from datetime import timedelta
from decimal import Decimal
from hashlib import sha256
from types import MappingProxyType

from pydantic import AwareDatetime, BaseModel, ConfigDict, field_validator

from envoy.domain.canonical import encode
from envoy.domain.ids import OfferHash

type IssueValue = Decimal | int | str | AwareDatetime | timedelta


class Offer(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)
    values: Mapping[str, IssueValue]

    @field_validator("values", mode="before")
    @classmethod
    def reject_floats(cls, values: Mapping[str, IssueValue]) -> Mapping[str, IssueValue]:
        if any(isinstance(value, (float, bool)) for value in values.values()):
            raise ValueError("Offer values cannot be floats or booleans")
        return values

    @field_validator("values")
    @classmethod
    def freeze_values(cls, values: Mapping[str, IssueValue]) -> Mapping[str, IssueValue]:
        return MappingProxyType(dict(values))

    def canonical_bytes(self) -> bytes:
        return encode(self.values)

    def hash(self) -> OfferHash:
        return OfferHash(sha256(self.canonical_bytes()).digest())
