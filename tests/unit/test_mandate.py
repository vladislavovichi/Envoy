from copy import deepcopy

import pytest
from pydantic import ValidationError

from envoy.mandate.commitment import commit, verify_reveal
from envoy.mandate.schema import Mandate
from envoy.ports.rng import SeededRng


@pytest.fixture
def meeting_data() -> dict:
    return {
        "template": "meeting",
        "issues": [{"id": "time", "kind": "datetime"}, {"id": "place", "kind": "place"}],
        "utility": {
            "weights": {"time": 0.5, "place": 0.5},
            "value_functions": {
                "time": {"points": [[0, 0], [1, 1]]},
                "place": {"points": [[0, 1], [1, 0]]},
            },
        },
        "hard_constraints": [{"kind": "cmp", "issue": "place", "op": "in", "value": ["office"]}],
        "reservation": {"minimum_utility": 0.4},
        "strategy": {"kind": "boulware", "params": {}},
        "limits": {"max_rounds": 10},
        "escalation": {"always": ["deposit"]},
    }


def test_valid_meeting_mandate_parses(meeting_data: dict) -> None:
    mandate = Mandate.model_validate(meeting_data)
    assert mandate.issues[0].kind == "datetime"
    assert mandate.reservation.minimum_utility == 0.4


@pytest.mark.parametrize(
    "weights",
    [
        {"time": 0.4, "place": 0.4},
        {"time": -0.1, "place": 1.1},
        {"time": float("nan"), "place": 0.5},
    ],
)
def test_invalid_weights_rejected(meeting_data: dict, weights: dict) -> None:
    meeting_data["utility"]["weights"] = weights
    with pytest.raises(ValidationError):
        Mandate.model_validate(meeting_data)


@pytest.mark.parametrize("delta", [1e-9, -1e-9])
def test_weights_sum_tolerance_1e_9_accepted(meeting_data: dict, delta: float) -> None:
    meeting_data["utility"]["weights"]["time"] += delta
    Mandate.model_validate(meeting_data)


def test_unknown_issue_in_constraint_rejected(meeting_data: dict) -> None:
    meeting_data["hard_constraints"][0]["issue"] = "missing"
    with pytest.raises(ValidationError, match="issue"):
        Mandate.model_validate(meeting_data)


def test_extra_field_rejected(meeting_data: dict) -> None:
    meeting_data["extra"] = "x"
    with pytest.raises(ValidationError):
        Mandate.model_validate(meeting_data)


def test_mandate_is_immutable(meeting_data: dict) -> None:
    mandate = Mandate.model_validate(meeting_data)
    with pytest.raises(ValidationError):
        mandate.template = "purchase"
    with pytest.raises(TypeError):
        mandate.utility.weights["time"] = 1


def test_json_schema_contains_all_issue_kinds() -> None:
    schema = str(Mandate.json_schema())
    for kind in ["money", "datetime", "place", "enum", "integer", "duration"]:
        assert kind in schema


def test_mandate_schema_snapshot(snapshot) -> None:
    assert Mandate.json_schema() == snapshot


def test_commit_reveal_roundtrip(meeting_data: dict) -> None:
    mandate = Mandate.model_validate(meeting_data)
    commitment = commit(mandate, SeededRng(5))
    assert verify_reveal(commitment.digest, mandate, commitment.salt)
    assert not verify_reveal(commitment.digest, mandate, b"wrong")


def test_reveal_with_modified_mandate_fails(meeting_data: dict) -> None:
    mandate = Mandate.model_validate(meeting_data)
    commitment = commit(mandate, SeededRng(5))
    changed = deepcopy(meeting_data)
    changed["reservation"]["minimum_utility"] = 0.7
    assert not verify_reveal(commitment.digest, Mandate.model_validate(changed), commitment.salt)


def test_constraint_values_cannot_mutate_signed_mandate(meeting_data: dict) -> None:
    mandate = Mandate.model_validate(meeting_data)
    commitment = commit(mandate, SeededRng(1))
    meeting_data["hard_constraints"][0]["value"].append("outside")
    with pytest.raises((AttributeError, TypeError)):
        mandate.hard_constraints[0].value.append("outside")
    assert verify_reveal(commitment.digest, mandate, commitment.salt)


@pytest.mark.parametrize("where", ["issue", "deadline"])
def test_naive_datetime_rejected_before_commitment(meeting_data, where) -> None:
    if where == "issue":
        meeting_data["issues"][0]["minimum"] = "2026-11-16T12:00:00"
    else:
        meeting_data["limits"]["deadline"] = "2026-11-16T12:00:00"
    with pytest.raises(ValidationError):
        Mandate.model_validate(meeting_data)
