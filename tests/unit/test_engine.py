import numpy as np
import pytest

from envoy.domain.offer import Offer
from envoy.engine.engine import Accept, Counter, NegotiationEngine, Reject
from envoy.engine.opponent import LastOfferProximity
from envoy.engine.pareto import pareto_front
from envoy.engine.strategy import (
    StrategyContext,
    TimeDependentStrategy,
    strategies,
    strategy_from_spec,
)
from envoy.mandate.compiler import compile_mandate
from envoy.mandate.grid import CandidateGrid
from envoy.ports.registry import UnknownImplementation
from tests.support.mandates import PLACES, SLOTS, meeting_mandate


def engine(**overrides: object) -> NegotiationEngine:
    compiled = compile_mandate(meeting_mandate(**overrides))
    return NegotiationEngine(compiled, strategy_from_spec(compiled.strategy), LastOfferProximity())


def offer(slot: int, place: str) -> Offer:
    return Offer(values={"time": SLOTS[slot], "place": place})


def test_boulware_concedes_slower_than_conceder() -> None:
    ctx = StrategyContext(progress=0.5, u_min=0.2, u_max=1.0, history=())
    assert TimeDependentStrategy(0.3).target_utility(ctx) > TimeDependentStrategy(3).target_utility(
        ctx
    )


@pytest.mark.parametrize("beta", [0.2, 1.0, 4.0])
def test_target_at_progress_zero_is_u_max_and_at_one_is_u_min(beta: float) -> None:
    strategy = TimeDependentStrategy(beta)
    assert strategy.target_utility(StrategyContext(0.0, 0.3, 0.9, ())) == pytest.approx(0.9)
    assert strategy.target_utility(StrategyContext(1.0, 0.3, 0.9, ())) == pytest.approx(0.3)
    assert strategy.target_utility(StrategyContext(2.0, 0.3, 0.9, ())) == pytest.approx(0.3)


def test_non_positive_beta_rejected() -> None:
    with pytest.raises(ValueError, match="beta"):
        TimeDependentStrategy(0)


def test_strategy_registry_selects_by_spec_kind() -> None:
    assert isinstance(
        strategy_from_spec(compile_mandate(meeting_mandate()).strategy), TimeDependentStrategy
    )
    with pytest.raises(UnknownImplementation):
        strategies.create("zeuthen")


def test_first_move_counters_with_max_own_utility() -> None:
    decision = engine().decide(None, (), 0)
    assert decision == Counter(offer(0, "office"))


def test_accepts_incoming_when_not_worse_than_own_counter() -> None:
    incoming = offer(0, "office")
    assert engine().decide(incoming, (incoming,), 1) == Accept(incoming)


def test_counters_when_incoming_infeasible() -> None:
    incoming = offer(0, "station")
    decision = engine().decide(incoming, (incoming,), 1)
    assert isinstance(decision, Counter)
    assert decision.offer.values["place"] != "station"


def test_counters_when_incoming_out_of_domain() -> None:
    incoming = Offer(values={"time": SLOTS[0], "place": "moon"})
    assert isinstance(engine().decide(incoming, (incoming,), 1), Counter)


def test_rejects_when_no_feasible_offer() -> None:
    blocked = engine(
        hard_constraints=[{"kind": "cmp", "issue": "place", "op": "in", "value": ["nowhere"]}]
    )
    assert blocked.decide(None, (), 0) == Reject("no_feasible_offer")
    above = engine(
        reservation={"minimum_utility": 1.0},
        hard_constraints=[{"kind": "cmp", "issue": "place", "op": "in", "value": ["cafe"]}],
    )
    assert above.decide(None, (), 0) == Reject("no_feasible_offer")


def test_final_round_accepts_only_above_reservation() -> None:
    subject = engine()
    good = offer(1, "cafe")
    bad = offer(7, "park")
    assert subject.decide(good, (good,), 6) == Accept(good)
    assert subject.decide(bad, (bad,), 6) == Reject("round_limit")
    assert subject.decide(None, (), 6) == Reject("round_limit")


def test_mid_negotiation_counter_moves_toward_opponent() -> None:
    subject = engine()
    incoming = offer(6, "park")
    decision = subject.decide(incoming, (incoming,), 4)
    assert isinstance(decision, Counter)
    assert decision.offer != offer(0, "office")


def test_tie_break_is_deterministic() -> None:
    flat = meeting_mandate(
        utility={
            "weights": {"time": 0.5, "place": 0.5},
            "value_functions": {
                "time": {"points": [[0, 1], [1, 1]]},
                "place": {"points": [[0, 1], [3, 1]]},
            },
        }
    )
    compiled = compile_mandate(flat)
    subject = NegotiationEngine(compiled, TimeDependentStrategy(1), LastOfferProximity())
    results = [subject.decide(None, (), 0) for _ in range(5)]
    assert results == [Counter(offer(0, "office"))] * 5


def test_pareto_front_drops_dominated_points() -> None:
    own = np.array([0.9, 0.9, 0.5, 0.4, 0.5, 0.1])
    opp = np.array([0.1, 0.2, 0.5, 0.4, 0.5, 0.9])
    mask = np.array([True, True, True, True, True, True])
    assert pareto_front(own, opp, mask).tolist() == [1, 2, 4, 5]
    assert pareto_front(own, opp, np.zeros(6, dtype=bool)).tolist() == []
    partial = np.array([True, False, False, True, False, False])
    assert pareto_front(own, opp, partial).tolist() == [0, 3]


def test_pareto_front_equal_opp_lower_own_dominated() -> None:
    own = np.array([0.9, 0.5])
    opp = np.array([0.3, 0.3])
    assert pareto_front(own, opp, np.ones(2, dtype=bool)).tolist() == [0]


def test_last_offer_proximity_prefers_equal_issues() -> None:
    grid = CandidateGrid(meeting_mandate().issues)
    model = LastOfferProximity()
    assert not model.utilities(grid, ()).any()
    scores = model.utilities(grid, (offer(2, "park"),))
    assert scores[2, PLACES.index("park")] == 1.0
    assert scores[2, 0] == 0.5
    assert scores[0, PLACES.index("park")] == 0.5
    assert scores[0, 0] == 0.0
    partial = model.utilities(grid, (Offer(values={"place": "cafe", "time": "soon"}),))
    assert partial[0, PLACES.index("cafe")] == 0.5
    assert partial.max() == 0.5
    missing = model.utilities(grid, (Offer(values={"place": "cafe"}),))
    assert missing.max() == 0.5
