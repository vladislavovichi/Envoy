from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol

from envoy.domain.offer import Offer
from envoy.mandate.schema import StrategySpec
from envoy.ports.registry import Registry


@dataclass(frozen=True)
class StrategyContext:
    progress: float
    u_min: float
    u_max: float
    history: Sequence[Offer]


class NegotiationStrategy(Protocol):
    def target_utility(self, ctx: StrategyContext) -> float: ...


class TimeDependentStrategy:
    def __init__(self, beta: float) -> None:
        if not beta > 0:
            raise ValueError("beta must be positive")
        self._beta = beta

    def target_utility(self, ctx: StrategyContext) -> float:
        progress = min(max(ctx.progress, 0.0), 1.0)
        return ctx.u_min + (ctx.u_max - ctx.u_min) * (1 - float(progress ** (1 / self._beta)))


strategies = Registry[NegotiationStrategy]()
strategies.register("time_dependent", TimeDependentStrategy)


def strategy_from_spec(
    spec: StrategySpec, registry: Registry[NegotiationStrategy] = strategies
) -> NegotiationStrategy:
    return registry.create(spec.kind, **dict(spec.params))
