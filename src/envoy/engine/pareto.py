import numpy as np
from numpy.typing import NDArray


def pareto_front(
    own: NDArray[np.float64], opp: NDArray[np.float64], mask: NDArray[np.bool_]
) -> NDArray[np.intp]:
    candidates = np.flatnonzero(mask)
    if candidates.size == 0:
        return candidates
    own_values = own.ravel()[candidates]
    opp_values = opp.ravel()[candidates]
    order = np.lexsort((-opp_values, -own_values))
    own_sorted = own_values[order]
    opp_sorted = opp_values[order]
    group_start = np.r_[True, own_sorted[1:] != own_sorted[:-1]]
    group_ids = np.cumsum(group_start) - 1
    group_best = opp_sorted[group_start]
    best_in_group = opp_sorted == group_best[group_ids]
    previous_best = np.r_[-np.inf, np.maximum.accumulate(group_best)[:-1]]
    survives = best_in_group & (opp_sorted > previous_best[group_ids])
    return np.sort(candidates[order[survives]])
