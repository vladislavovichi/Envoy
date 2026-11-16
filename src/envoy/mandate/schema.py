from collections.abc import Mapping
from datetime import timedelta
from decimal import Decimal
from math import isfinite
from types import MappingProxyType
from typing import Annotated, Any, Literal, Self

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    field_serializer,
    field_validator,
    model_validator,
)

from envoy.domain.canonical import encode, normalize


class FrozenModel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", allow_inf_nan=False)


class MoneyIssue(FrozenModel):
    id: str
    kind: Literal["money"]
    currency: str = "RUB"
    minimum: Decimal | None = None
    maximum: Decimal | None = None


class DatetimeIssue(FrozenModel):
    id: str
    kind: Literal["datetime"]
    minimum: AwareDatetime | None = None
    maximum: AwareDatetime | None = None


class PlaceIssue(FrozenModel):
    id: str
    kind: Literal["place"]
    choices: tuple[str, ...] = ()


class EnumIssue(FrozenModel):
    id: str
    kind: Literal["enum"]
    choices: tuple[str, ...]


class IntegerIssue(FrozenModel):
    id: str
    kind: Literal["integer"]
    minimum: int | None = None
    maximum: int | None = None


class DurationIssue(FrozenModel):
    id: str
    kind: Literal["duration"]
    minimum: timedelta | None = None
    maximum: timedelta | None = None


type Issue = Annotated[
    MoneyIssue | DatetimeIssue | PlaceIssue | EnumIssue | IntegerIssue | DurationIssue,
    Field(discriminator="kind"),
]


class PiecewiseLinear(FrozenModel):
    points: tuple[tuple[float, float], ...] = Field(min_length=2)

    @model_validator(mode="after")
    def ordered_points(self) -> Self:
        if any(
            left[0] >= right[0] for left, right in zip(self.points, self.points[1:], strict=False)
        ):
            raise ValueError("Piecewise points must have strictly increasing coordinates")
        if any(not 0 <= point[1] <= 1 for point in self.points):
            raise ValueError("Value function must stay in [0, 1]")
        return self


class UtilitySpec(FrozenModel):
    weights: Mapping[str, float]
    value_functions: Mapping[str, PiecewiseLinear]

    @model_validator(mode="after")
    def valid_weights(self) -> Self:
        if any(not isfinite(weight) or weight < 0 for weight in self.weights.values()):
            raise ValueError("Weights must be finite and nonnegative")
        total = sum((Decimal(str(value)) for value in self.weights.values()), Decimal(0))
        if abs(total - 1) > Decimal("0.000000001"):
            raise ValueError("Weights must sum to one")
        if self.weights.keys() != self.value_functions.keys():
            raise ValueError("Weights and value functions must refer to the same issues")
        object.__setattr__(self, "weights", MappingProxyType(dict(self.weights)))
        object.__setattr__(self, "value_functions", MappingProxyType(dict(self.value_functions)))
        return self

    @field_serializer("weights", "value_functions")
    def serialize_mapping(self, value: Mapping[str, Any]) -> dict[str, Any]:
        return dict(value)


class Cmp(FrozenModel):
    kind: Literal["cmp"] = "cmp"
    issue: str
    op: Literal["<=", ">=", "in", "between", "not_in"]
    value: JsonValue

    @field_validator("value")
    @classmethod
    def freeze_value(cls, value: JsonValue) -> Any:
        def freeze(item: Any) -> Any:
            if isinstance(item, dict):
                return MappingProxyType({key: freeze(nested) for key, nested in item.items()})
            if isinstance(item, list):
                return tuple(freeze(nested) for nested in item)
            return item

        return freeze(value)

    @field_serializer("value")
    def serialize_value(self, value: Any) -> Any:
        return normalize(value)


class And(FrozenModel):
    kind: Literal["and"]
    predicates: tuple[Predicate, ...] = Field(min_length=1)


class Or(FrozenModel):
    kind: Literal["or"]
    predicates: tuple[Predicate, ...] = Field(min_length=1)


class Not(FrozenModel):
    kind: Literal["not"]
    predicate: Predicate


class Requires(FrozenModel):
    kind: Literal["requires"]
    condition: Predicate
    consequence: Predicate


type Predicate = Annotated[Cmp | And | Or | Not | Requires, Field(discriminator="kind")]


def referenced_issues(predicate: Predicate) -> set[str]:
    match predicate.kind:
        case "cmp":
            return {predicate.issue}
        case "and" | "or":
            return set().union(*(referenced_issues(item) for item in predicate.predicates))
        case "not":
            return referenced_issues(predicate.predicate)
        case "requires":
            return referenced_issues(predicate.condition) | referenced_issues(predicate.consequence)


class Reservation(FrozenModel):
    minimum_utility: float = Field(ge=0, le=1)
    constraints: tuple[Predicate, ...] = ()


class StrategySpec(FrozenModel):
    kind: str
    params: Mapping[str, float | int | str | bool] = Field(default_factory=dict)

    @field_validator("params")
    @classmethod
    def freeze_params(cls, value: Mapping[str, Any]) -> Mapping[str, Any]:
        return MappingProxyType(dict(value))

    @field_serializer("params")
    def serialize_params(self, value: Mapping[str, Any]) -> dict[str, Any]:
        return dict(value)


class Limits(FrozenModel):
    max_rounds: int = Field(gt=0)
    deadline: AwareDatetime | None = None
    max_deals_per_day: int = Field(default=1, ge=0)
    max_deposit: int = Field(default=0, ge=0)
    allow: tuple[str, ...] = ()
    deny: tuple[str, ...] = ()


class Escalation(FrozenModel):
    always: tuple[str, ...] = ()


class Mandate(FrozenModel):
    version: int = Field(default=1, ge=1)
    template: Literal["meeting", "purchase", "advertisement", "exchange", "join", "freelance"]
    issues: tuple[Issue, ...] = Field(min_length=1)
    utility: UtilitySpec
    hard_constraints: tuple[Predicate, ...] = ()
    reservation: Reservation
    strategy: StrategySpec
    limits: Limits
    escalation: Escalation

    @model_validator(mode="after")
    def check_issues(self) -> Self:
        ids = {issue.id for issue in self.issues}
        if len(ids) != len(self.issues):
            raise ValueError("Duplicate issue ids")
        if ids != self.utility.weights.keys():
            raise ValueError("Utility keys must match issue ids")
        for predicate in self.hard_constraints + self.reservation.constraints:
            if not referenced_issues(predicate) <= ids:
                raise ValueError("Constraint refers to an unknown issue")
        return self

    @classmethod
    def json_schema(cls) -> dict[str, Any]:
        return cls.model_json_schema()

    def canonical_bytes(self) -> bytes:
        return encode(self.model_dump(mode="python"))
