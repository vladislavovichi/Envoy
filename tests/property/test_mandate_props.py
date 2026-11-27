from hypothesis import given
from hypothesis import strategies as st

from envoy.mandate.schema import Mandate


@given(st.permutations(["a", "b", "c"]))
def test_canonical_bytes_independent_of_construction_order(order: list[str]) -> None:
    def build(keys: list[str]) -> Mandate:
        return Mandate.model_validate(
            {
                "template": "purchase",
                "issues": [
                    {"kind": "integer", "id": key, "minimum": 0, "maximum": 3}
                    for key in ["a", "b", "c"]
                ],
                "utility": {
                    "weights": {key: 1 / 3 for key in keys},
                    "value_functions": {key: {"points": [[0, 0], [1, 1]]} for key in keys},
                },
                "reservation": {"minimum_utility": 0.5},
                "strategy": {"kind": "time_dependent"},
                "limits": {"max_rounds": 5},
                "escalation": {"always": []},
            }
        )

    assert build(order).canonical_bytes() == build(["a", "b", "c"]).canonical_bytes()
