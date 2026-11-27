from datetime import UTC, datetime, timedelta

from envoy.mandate.schema import Mandate

SLOTS = tuple(datetime(2026, 12, 1, 9, tzinfo=UTC) + timedelta(hours=hour) for hour in range(8))
PLACES = ("office", "cafe", "park", "station")
RESERVATION = 0.37
WEIGHT_TIME = 0.6123
WEIGHT_PLACE = 0.3877


def meeting_data(**overrides: object) -> dict:
    first = SLOTS[0].timestamp()
    last = SLOTS[-1].timestamp()
    data: dict = {
        "template": "meeting",
        "issues": [
            {"id": "time", "kind": "datetime", "choices": [slot.isoformat() for slot in SLOTS]},
            {"id": "place", "kind": "place", "choices": list(PLACES)},
        ],
        "utility": {
            "weights": {"time": WEIGHT_TIME, "place": WEIGHT_PLACE},
            "value_functions": {
                "time": {"points": [[first, 1.0], [last, 0.0]]},
                "place": {"points": [[0, 1.0], [3, 0.0]]},
            },
        },
        "hard_constraints": [
            {"kind": "cmp", "issue": "place", "op": "not_in", "value": ["station"]}
        ],
        "reservation": {"minimum_utility": RESERVATION},
        "strategy": {"kind": "time_dependent", "params": {"beta": 0.5}},
        "limits": {"max_rounds": 6},
        "escalation": {"always": []},
    }
    data.update(overrides)
    return data


def meeting_mandate(**overrides: object) -> Mandate:
    return Mandate.model_validate(meeting_data(**overrides))
