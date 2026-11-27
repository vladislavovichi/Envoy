from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Protocol

from pydantic import JsonValue

from envoy.domain.offer import IssueValue, Offer


class Projection[T](Protocol):
    def project(self, value: T) -> Mapping[str, JsonValue]: ...


def project_value(value: IssueValue) -> JsonValue:
    match value:
        case datetime():
            return value.isoformat()
        case timedelta():
            return int(value.total_seconds())
        case Decimal():
            return str(value)
        case _:
            return value


class OfferProjection:
    def project(self, value: Offer) -> Mapping[str, JsonValue]:
        return {issue: project_value(item) for issue, item in sorted(value.values.items())}


class ClarificationProjection:
    def project(self, value: Sequence[str]) -> Mapping[str, JsonValue]:
        missing: list[JsonValue] = [name for name in sorted(set(value))]
        return {"missing": missing}


@dataclass(frozen=True)
class OwnerCard:
    offer: Offer | None
    reason: str


class OwnerCardProjection:
    def __init__(self) -> None:
        self._offers = OfferProjection()

    def project(self, value: OwnerCard) -> Mapping[str, JsonValue]:
        offer = None if value.offer is None else dict(self._offers.project(value.offer))
        return {"offer": offer, "reason": value.reason}
