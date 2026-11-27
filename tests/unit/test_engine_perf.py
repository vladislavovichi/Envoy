import time

from envoy.engine.engine import Counter, NegotiationEngine
from envoy.engine.opponent import LastOfferProximity
from envoy.engine.strategy import TimeDependentStrategy
from envoy.mandate.compiler import compile_mandate
from envoy.mandate.schema import Mandate

SIZES = (6, 6, 6, 5, 5, 5, 4, 4)


def large_mandate() -> Mandate:
    issues = [
        {"id": f"i{axis}", "kind": "integer", "minimum": 0, "maximum": size - 1}
        for axis, size in enumerate(SIZES)
    ]
    return Mandate.model_validate(
        {
            "template": "purchase",
            "issues": issues,
            "utility": {
                "weights": {issue["id"]: 1 / len(SIZES) for issue in issues},
                "value_functions": {
                    issue["id"]: {"points": [[0, 1], [issue["maximum"], 0]]} for issue in issues
                },
            },
            "hard_constraints": [
                {"kind": "cmp", "issue": "i0", "op": "<=", "value": 3},
                {
                    "kind": "requires",
                    "condition": {"kind": "cmp", "issue": "i1", "op": ">=", "value": 2},
                    "consequence": {"kind": "cmp", "issue": "i2", "op": "not_in", "value": [4]},
                },
            ],
            "reservation": {"minimum_utility": 0.3},
            "strategy": {"kind": "time_dependent", "params": {"beta": 0.5}},
            "limits": {"max_rounds": 10},
            "escalation": {},
        }
    )


def test_decide_under_two_seconds_for_eight_issues_300k_cells() -> None:
    started = time.perf_counter()
    compiled = compile_mandate(large_mandate())
    assert compiled.grid.size >= 300_000
    engine = NegotiationEngine(compiled, TimeDependentStrategy(0.5), LastOfferProximity())
    incoming = compiled.grid.offer_at(tuple(size - 1 for size in SIZES))
    decision = engine.decide(incoming, (incoming,), 5)
    elapsed = time.perf_counter() - started
    assert isinstance(decision, Counter)
    assert elapsed < 2.0, elapsed
