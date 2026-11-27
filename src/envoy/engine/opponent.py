from collections.abc import Sequence
from typing import Protocol

import numpy as np
from numpy.typing import NDArray

from envoy.domain.offer import Offer
from envoy.mandate.grid import CandidateGrid


class OpponentModel(Protocol):
    def utilities(self, grid: CandidateGrid, observed: Sequence[Offer]) -> NDArray[np.float64]: ...


class LastOfferProximity:
    def utilities(self, grid: CandidateGrid, observed: Sequence[Offer]) -> NDArray[np.float64]:
        total = np.zeros(grid.shape, dtype=np.float64)
        if not observed:
            return total
        last = observed[-1]
        dimensions = len(grid.shape)
        for axis, issue_id in enumerate(grid.ids):
            if issue_id not in last.values:
                continue
            position = grid.position(axis, last.values[issue_id])
            if position is None:
                continue
            matches = np.zeros(grid.shape[axis], dtype=np.float64)
            matches[position] = 1.0
            total = total + matches.reshape(
                tuple(-1 if other == axis else 1 for other in range(dimensions))
            )
        return total / dimensions
