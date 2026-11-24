from hypothesis import given, settings
from hypothesis import strategies as st

from envoy.engine.engine import Accept, Counter, NegotiationEngine, Reject
from envoy.engine.opponent import LastOfferProximity
from envoy.engine.strategy import strategy_from_spec
from envoy.mandate.compiler import compile_mandate
from envoy.mandate.schema import Mandate
from tests.strategies import mandates


def build(mandate: Mandate) -> NegotiationEngine:
    compiled = compile_mandate(mandate)
    return NegotiationEngine(compiled, strategy_from_spec(compiled.strategy), LastOfferProximity())


@settings(max_examples=60, deadline=None)
@given(mandates(), st.data())
def test_every_counter_satisfies_constraints_and_reservation(mandate: Mandate, data) -> None:
    compiled = compile_mandate(mandate)
    engine = build(mandate)
    history = []
    for round in range(compiled.limits.max_rounds + 1):
        index = tuple(data.draw(st.integers(0, size - 1)) for size in compiled.grid.shape)
        incoming = data.draw(st.one_of(st.none(), st.just(compiled.grid.offer_at(index))))
        if incoming is not None:
            history.append(incoming)
        decision = engine.decide(incoming, tuple(history), round)
        if isinstance(decision, (Counter, Accept)):
            assert compiled.constraints.is_feasible(decision.offer)
            assert compiled.utility.value(decision.offer) >= compiled.reservation - 1e-9
        if isinstance(decision, Accept):
            assert decision.offer == incoming
        if isinstance(decision, Reject):
            assert decision.reason in {"no_feasible_offer", "round_limit"}


@settings(max_examples=60, deadline=None)
@given(mandates())
def test_target_non_increasing_over_rounds(mandate: Mandate) -> None:
    engine = build(mandate)
    rounds = compile_mandate(mandate).limits.max_rounds
    targets = [engine.target(round, ()) for round in range(rounds + 2)]
    assert all(
        later <= earlier + 1e-12 for earlier, later in zip(targets, targets[1:], strict=False)
    )


@settings(max_examples=40, deadline=None)
@given(mandates(), st.integers(0, 25), st.data())
def test_decide_is_deterministic(mandate: Mandate, round: int, data) -> None:
    compiled = compile_mandate(mandate)
    index = tuple(data.draw(st.integers(0, size - 1)) for size in compiled.grid.shape)
    incoming = compiled.grid.offer_at(index)
    first = build(mandate).decide(incoming, (incoming,), round)
    second = build(mandate).decide(incoming, (incoming,), round)
    assert first == second
