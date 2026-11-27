from collections.abc import Mapping
from datetime import datetime, timedelta
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
    TypeAdapter,
    field_serializer,
    field_validator,
    model_validator,
)

from envoy.domain.canonical import encode, normalize
from envoy.domain.offer import IssueValue

MAX_GRID_CELLS = 1_000_000
_STR = TypeAdapter(str)
_INT = TypeAdapter(int)
_DECIMAL = TypeAdapter(Decimal)
_TIMEDELTA = TypeAdapter(timedelta)
_DATETIME = TypeAdapter(AwareDatetime)


class FrozenModel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", allow_inf_nan=False)


class _IssueBase(FrozenModel):
    id: str = Field(min_length=1, max_length=64)

    def values(self) -> tuple[IssueValue, ...]:
        raise NotImplementedError

    def size(self) -> int:
        return len(self.values())

    def coerce(self, raw: Any) -> IssueValue:
        raise NotImplementedError

    def as_number(self, value: IssueValue) -> float:
        raise NotImplementedError


class _ChoiceIssue(_IssueBase):
    choices: tuple[str, ...] = Field(min_length=1)

    @field_validator("choices")
    @classmethod
    def unique_choices(cls, choices: tuple[str, ...]) -> tuple[str, ...]:
        if len(set(choices)) != len(choices):
            raise ValueError("Choices must be unique")
        return choices

    def values(self) -> tuple[IssueValue, ...]:
        return self.choices

    def size(self) -> int:
        return len(self.choices)

    def coerce(self, raw: Any) -> IssueValue:
        return _STR.validate_python(raw)

    def as_number(self, value: IssueValue) -> float:
        if not isinstance(value, str):
            raise ValueError("Choice value must be a string")
        return float(self.choices.index(value))


class _RangeIssue[N: (int, Decimal, timedelta)](_IssueBase):
    minimum: N
    maximum: N
    step: N

    @model_validator(mode="after")
    def ordered_range(self) -> Self:
        if self.step <= self.step * 0 or self.minimum > self.maximum:
            raise ValueError("Range requires minimum <= maximum and a positive step")
        return self

    def size(self) -> int:
        return int((self.maximum - self.minimum) // self.step) + 1

    def values(self) -> tuple[IssueValue, ...]:
        return tuple(self.minimum + self.step * index for index in range(self.size()))


class MoneyIssue(_RangeIssue[Decimal]):
    kind: Literal["money"]
    currency: str = "RUB"

    def coerce(self, raw: Any) -> IssueValue:
        return _DECIMAL.validate_python(raw)

    def as_number(self, value: IssueValue) -> float:
        if not isinstance(value, (Decimal, int)) or isinstance(value, bool):
            raise ValueError("Money value must be a decimal")
        return float(value)


class IntegerIssue(_RangeIssue[int]):
    kind: Literal["integer"]
    step: int = 1

    def coerce(self, raw: Any) -> IssueValue:
        return _INT.validate_python(raw)

    def as_number(self, value: IssueValue) -> float:
        if not isinstance(value, int) or isinstance(value, bool):
            raise ValueError("Integer value must be an int")
        return float(value)


class DurationIssue(_RangeIssue[timedelta]):
    kind: Literal["duration"]

    def coerce(self, raw: Any) -> IssueValue:
        return _TIMEDELTA.validate_python(raw)

    def as_number(self, value: IssueValue) -> float:
        if not isinstance(value, timedelta):
            raise ValueError("Duration value must be a timedelta")
        return value.total_seconds()


class DatetimeIssue(_IssueBase):
    kind: Literal["datetime"]
    choices: tuple[AwareDatetime, ...] = Field(min_length=1)

    @field_validator("choices")
    @classmethod
    def unique_choices(cls, choices: tuple[datetime, ...]) -> tuple[datetime, ...]:
        if len(set(choices)) != len(choices):
            raise ValueError("Choices must be unique")
        return choices

    def values(self) -> tuple[IssueValue, ...]:
        return self.choices

    def size(self) -> int:
        return len(self.choices)

    def coerce(self, raw: Any) -> IssueValue:
        return _DATETIME.validate_python(raw)

    def as_number(self, value: IssueValue) -> float:
        if not isinstance(value, datetime):
            raise ValueError("Datetime value must be an aware datetime")
        return value.timestamp()


class PlaceIssue(_ChoiceIssue):
    kind: Literal["place"]


class EnumIssue(_ChoiceIssue):
    kind: Literal["enum"]


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


def check_constraint_values(predicate: Predicate, issues: Mapping[str, Issue]) -> None:
    match predicate.kind:
        case "cmp":
            issue = issues[predicate.issue]
            value = predicate.value
            if predicate.op in ("<=", ">="):
                issue.as_number(issue.coerce(value))
                return
            if not isinstance(value, (tuple, list)):
                raise ValueError(f"Operator {predicate.op} expects a list")
            if predicate.op == "between":
                if len(value) != 2:
                    raise ValueError("between expects exactly two bounds")
                for bound in value:
                    issue.as_number(issue.coerce(bound))
            else:
                for item in value:
                    issue.coerce(item)
        case "and" | "or":
            for nested in predicate.predicates:
                check_constraint_values(nested, issues)
        case "not":
            check_constraint_values(predicate.predicate, issues)
        case "requires":
            check_constraint_values(predicate.condition, issues)
            check_constraint_values(predicate.consequence, issues)


class Reservation(FrozenModel):
    minimum_utility: float = Field(ge=0, le=1)
    constraints: tuple[Predicate, ...] = ()


class StrategySpec(FrozenModel):
    kind: Literal["time_dependent"] = "time_dependent"
    params: Mapping[str, float] = Field(default_factory=lambda: {"beta": 0.5})

    @field_validator("params")
    @classmethod
    def freeze_params(cls, value: Mapping[str, float]) -> Mapping[str, float]:
        if any(not isfinite(item) for item in value.values()):
            raise ValueError("Strategy parameters must be finite")
        if value.get("beta", 1.0) <= 0:
            raise ValueError("beta must be positive")
        return MappingProxyType(dict(value))

    @field_serializer("params")
    def serialize_params(self, value: Mapping[str, float]) -> dict[str, float]:
        return dict(value)


class Limits(FrozenModel):
    max_rounds: int = Field(gt=0)
    session_deadline: timedelta = Field(default=timedelta(days=2), gt=timedelta(0))
    max_deals_per_day: int = Field(default=1, ge=0)
    max_deposit: int = Field(default=0, ge=0)
    counterparts_allow: frozenset[int] | None = None
    counterparts_deny: frozenset[int] = frozenset()

    @field_serializer("counterparts_allow", "counterparts_deny")
    def serialize_ids(self, value: frozenset[int] | None) -> list[int] | None:
        return None if value is None else sorted(value)


class Escalation(FrozenModel):
    always: frozenset[Literal["accept_deal"]] = frozenset()
    timeout: timedelta = Field(default=timedelta(hours=1), gt=timedelta(0))

    @field_serializer("always")
    def serialize_always(self, value: frozenset[str]) -> list[str]:
        return sorted(value)


MEETING_ISSUES = {"time": "datetime", "place": "place"}


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
            check_constraint_values(predicate, {issue.id: issue for issue in self.issues})
        if (
            self.template == "meeting"
            and {issue.id: issue.kind for issue in self.issues} != MEETING_ISSUES
        ):
            raise ValueError("Meeting template requires issues time:datetime and place:place")
        cells = 1
        for issue in self.issues:
            cells *= issue.size()
            if cells > MAX_GRID_CELLS:
                raise ValueError(f"Candidate grid exceeds {MAX_GRID_CELLS} cells")
        return self

    @classmethod
    def json_schema(cls) -> dict[str, Any]:
        return cls.model_json_schema()

    def canonical_bytes(self) -> bytes:
        return encode(self.model_dump(mode="python"))
