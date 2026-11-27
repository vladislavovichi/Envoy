from collections.abc import Callable, Sequence
from dataclasses import dataclass
from functools import cached_property, reduce
from typing import Any

import numpy as np
from numpy.typing import NDArray
from pydantic import ValidationError

from envoy.domain.offer import Offer
from envoy.mandate.grid import CandidateGrid
from envoy.mandate.schema import (
    Escalation,
    Issue,
    Limits,
    Mandate,
    Predicate,
    StrategySpec,
    UtilitySpec,
)

type Axes = Sequence[NDArray[np.float64]]
type MaskFunction = Callable[[Axes], NDArray[np.bool_]]


class MandateCompileError(ValueError):
    pass


class OutsideDomain(ValueError):
    pass


def _number(issue: Issue, raw: Any) -> float:
    try:
        return issue.as_number(issue.coerce(raw))
    except ValidationError, ValueError, TypeError:
        raise MandateCompileError(f"Value {raw!r} is invalid for issue {issue.id}") from None


def _numbers(issue: Issue, raw: Any, *, skip_unknown: bool) -> list[float]:
    if not isinstance(raw, (tuple, list)):
        raise MandateCompileError(f"Issue {issue.id} expects a list of values")
    result: list[float] = []
    for item in raw:
        try:
            result.append(_number(issue, item))
        except MandateCompileError:
            if not skip_unknown:
                raise
    return result


def compile_predicate(predicate: Predicate, grid: CandidateGrid) -> MaskFunction:
    match predicate.kind:
        case "cmp":
            axis = grid.axis(predicate.issue)
            issue = grid.issues[axis]
            value = predicate.value
            match predicate.op:
                case "<=":
                    bound = _number(issue, value)
                    return lambda axes: axes[axis] <= bound
                case ">=":
                    bound = _number(issue, value)
                    return lambda axes: axes[axis] >= bound
                case "between":
                    low, high = _numbers(issue, value, skip_unknown=False)
                    return lambda axes: (axes[axis] >= low) & (axes[axis] <= high)
                case "in":
                    members = np.array(_numbers(issue, value, skip_unknown=True))
                    return lambda axes: np.isin(axes[axis], members)
                case "not_in":
                    excluded = np.array(_numbers(issue, value, skip_unknown=True))
                    return lambda axes: ~np.isin(axes[axis], excluded)
        case "and":
            parts = [compile_predicate(item, grid) for item in predicate.predicates]
            return lambda axes: reduce(np.logical_and, (part(axes) for part in parts))
        case "or":
            parts = [compile_predicate(item, grid) for item in predicate.predicates]
            return lambda axes: reduce(np.logical_or, (part(axes) for part in parts))
        case "not":
            inner = compile_predicate(predicate.predicate, grid)
            return lambda axes: ~inner(axes)
        case "requires":
            condition = compile_predicate(predicate.condition, grid)
            consequence = compile_predicate(predicate.consequence, grid)
            return lambda axes: ~condition(axes) | consequence(axes)
    raise MandateCompileError(f"Unsupported predicate {predicate.kind}")


class Constraints:
    def __init__(self, grid: CandidateGrid, predicates: Sequence[Predicate]) -> None:
        self._grid = grid
        self._functions = [compile_predicate(predicate, grid) for predicate in predicates]

    def _evaluate(self, axes: Axes, shape: tuple[int, ...]) -> NDArray[np.bool_]:
        result = np.ones(shape, dtype=np.bool_)
        for function in self._functions:
            result &= function(axes)
        return result

    @cached_property
    def _mask(self) -> NDArray[np.bool_]:
        mask = self._evaluate(self._grid.sparse_axes(), self._grid.shape)
        mask.setflags(write=False)
        return mask

    def mask(self) -> NDArray[np.bool_]:
        return self._mask

    def is_feasible(self, offer: Offer) -> bool:
        index = self._grid.index_of(offer)
        if index is None:
            return False
        point = self._evaluate(self._grid.point_axes(index), (1,) * len(index))
        return bool(point.item())


class Utility:
    def __init__(self, grid: CandidateGrid, spec: UtilitySpec) -> None:
        self._grid = grid
        self._terms = [
            (
                axis,
                spec.weights[issue_id],
                np.array([point[0] for point in spec.value_functions[issue_id].points]),
                np.array([point[1] for point in spec.value_functions[issue_id].points]),
            )
            for axis, issue_id in enumerate(grid.ids)
        ]

    def _evaluate(self, axes: Axes, shape: tuple[int, ...]) -> NDArray[np.float64]:
        total = np.zeros(shape, dtype=np.float64)
        for axis, weight, xs, ys in self._terms:
            total = total + weight * np.interp(axes[axis], xs, ys)
        return np.clip(total, 0.0, 1.0)

    @cached_property
    def _table(self) -> NDArray[np.float64]:
        table = self._evaluate(self._grid.sparse_axes(), self._grid.shape)
        table.setflags(write=False)
        return table

    def table(self) -> NDArray[np.float64]:
        return self._table

    def value(self, offer: Offer) -> float:
        index = self._grid.index_of(offer)
        if index is None:
            raise OutsideDomain("Offer is outside the mandate domain")
        return float(self._evaluate(self._grid.point_axes(index), (1,) * len(index)).item())


@dataclass(frozen=True)
class CompiledMandate:
    grid: CandidateGrid
    constraints: Constraints
    utility: Utility
    reservation: float
    limits: Limits
    escalation: Escalation
    strategy: StrategySpec
    template: str


def compile_mandate(mandate: Mandate) -> CompiledMandate:
    grid = CandidateGrid(mandate.issues)
    return CompiledMandate(
        grid=grid,
        constraints=Constraints(grid, mandate.hard_constraints + mandate.reservation.constraints),
        utility=Utility(grid, mandate.utility),
        reservation=mandate.reservation.minimum_utility,
        limits=mandate.limits,
        escalation=mandate.escalation,
        strategy=mandate.strategy,
        template=mandate.template,
    )
