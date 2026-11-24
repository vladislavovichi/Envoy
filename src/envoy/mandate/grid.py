from collections.abc import Hashable, Sequence
from decimal import Decimal

import numpy as np
from numpy.typing import NDArray

from envoy.domain.offer import IssueValue, Offer
from envoy.mandate.schema import Issue


class CandidateGrid:
    def __init__(self, issues: Sequence[Issue]) -> None:
        self.issues: tuple[Issue, ...] = tuple(issues)
        self.ids: tuple[str, ...] = tuple(issue.id for issue in self.issues)
        self._values = [issue.values() for issue in self.issues]
        self._lookup: list[dict[Hashable, int]] = [
            {value: index for index, value in enumerate(values)} for values in self._values
        ]
        self._numbers = [
            np.array([issue.as_number(value) for value in values], dtype=np.float64)
            for issue, values in zip(self.issues, self._values, strict=True)
        ]
        self.shape: tuple[int, ...] = tuple(len(values) for values in self._values)
        self.size = int(np.prod(self.shape, dtype=np.int64))

    def axis(self, issue_id: str) -> int:
        return self.ids.index(issue_id)

    def axis_numbers(self, i: int) -> NDArray[np.float64]:
        return self._numbers[i]

    def value_at(self, axis: int, position: int) -> IssueValue:
        return self._values[axis][position]

    def offer_at(self, index: tuple[int, ...]) -> Offer:
        return Offer(
            values={
                issue_id: self._values[axis][position]
                for axis, (issue_id, position) in enumerate(zip(self.ids, index, strict=True))
            }
        )

    def offer_at_flat(self, flat: int) -> Offer:
        return self.offer_at(self.unravel(flat))

    def unravel(self, flat: int) -> tuple[int, ...]:
        return tuple(int(item) for item in np.unravel_index(flat, self.shape))

    def position(self, axis: int, value: IssueValue) -> int | None:
        position = self._lookup[axis].get(value)
        if position is None or not _compatible(self._values[axis][position], value):
            return None
        return position

    def index_of(self, offer: Offer) -> tuple[int, ...] | None:
        if set(offer.values) != set(self.ids):
            return None
        index: list[int] = []
        for axis, issue_id in enumerate(self.ids):
            position = self.position(axis, offer.values[issue_id])
            if position is None:
                return None
            index.append(position)
        return tuple(index)

    def sparse_axes(self) -> list[NDArray[np.float64]]:
        dimensions = len(self.shape)
        return [
            numbers.reshape(tuple(-1 if axis == i else 1 for axis in range(dimensions)))
            for i, numbers in enumerate(self._numbers)
        ]

    def point_axes(self, index: tuple[int, ...]) -> list[NDArray[np.float64]]:
        dimensions = len(self.shape)
        return [
            numbers[position : position + 1].reshape((1,) * dimensions)
            for numbers, position in zip(self._numbers, index, strict=True)
        ]


def _compatible(stored: IssueValue, value: IssueValue) -> bool:
    if isinstance(stored, Decimal):
        return isinstance(value, (Decimal, int))
    return type(stored) is type(value)
