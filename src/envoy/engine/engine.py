from collections.abc import Sequence
from dataclasses import dataclass

import numpy as np

from envoy.domain.offer import Offer
from envoy.engine.opponent import OpponentModel
from envoy.engine.pareto import pareto_front
from envoy.engine.strategy import NegotiationStrategy, StrategyContext
from envoy.mandate.compiler import CompiledMandate

TOLERANCE = 1e-12


@dataclass(frozen=True)
class Accept:
    offer: Offer


@dataclass(frozen=True)
class Counter:
    offer: Offer


@dataclass(frozen=True)
class Reject:
    reason: str


type Decision = Accept | Counter | Reject


class NegotiationEngine:
    def __init__(
        self, compiled: CompiledMandate, strategy: NegotiationStrategy, opponent: OpponentModel
    ) -> None:
        self._compiled = compiled
        self._strategy = strategy
        self._opponent = opponent

    def _acceptable(self, offer: Offer | None) -> float | None:
        if offer is None or not self._compiled.constraints.is_feasible(offer):
            return None
        utility = self._compiled.utility.value(offer)
        return utility if utility >= self._compiled.reservation - TOLERANCE else None

    def target(self, round: int, history: Sequence[Offer]) -> float:
        compiled = self._compiled
        mask = compiled.constraints.mask()
        own = compiled.utility.table()
        u_max = float(own[mask].max()) if mask.any() else compiled.reservation
        progress = min(round / compiled.limits.max_rounds, 1.0)
        return self._strategy.target_utility(
            StrategyContext(
                progress, compiled.reservation, max(u_max, compiled.reservation), history
            )
        )

    def decide(
        self,
        incoming: Offer | None,
        history: Sequence[Offer],
        round: int,
        exclude: Sequence[Offer] = (),
    ) -> Decision:
        compiled = self._compiled
        own = compiled.utility.table()
        acceptable = compiled.constraints.mask() & (own >= compiled.reservation - TOLERANCE)
        if not acceptable.any():
            return Reject("no_feasible_offer")
        incoming_utility = self._acceptable(incoming)
        if round >= compiled.limits.max_rounds:
            if incoming is not None and incoming_utility is not None:
                return Accept(incoming)
            return Reject("round_limit")
        target = self.target(round, history)
        fresh = acceptable.copy()
        for offer in exclude:
            index = compiled.grid.index_of(offer)
            if index is not None:
                fresh[index] = False
        if not fresh.any():
            if incoming is not None and incoming_utility is not None:
                return Accept(incoming)
            return Reject("no_new_offer")
        candidates = fresh & (own >= target - TOLERANCE)
        if not candidates.any():
            candidates = fresh & (own >= float(own[fresh].max()) - TOLERANCE)
        opp = self._opponent.utilities(compiled.grid, history)
        front = pareto_front(own, opp, candidates)
        own_flat = own.ravel()[front]
        opp_flat = opp.ravel()[front]
        best = int(front[np.lexsort((front, -own_flat, -opp_flat))[0]])
        counter = compiled.grid.offer_at_flat(best)
        if incoming is not None and incoming_utility is not None:
            if incoming_utility >= float(own.ravel()[best]) - TOLERANCE:
                return Accept(incoming)
        return Counter(counter)
