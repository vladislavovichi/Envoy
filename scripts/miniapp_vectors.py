import base64
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from envoy.crypto.keys import SigningKey  # noqa: E402
from envoy.mandate.schema import Mandate  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
SCHEMA = ROOT / "miniapp" / "src" / "generated" / "mandate.schema.json"
VECTORS = ROOT / "miniapp" / "tests" / "fixtures" / "vectors.json"

MEETING_STATE = {
    "slots": ["2026-12-03T12:00", "2026-12-03T10:00", "2026-12-04T09:30"],
    "timezone": "Europe/Moscow",
    "places": ["кафе «Пушкин»", "офис"],
    "excluded": ["вокзал"],
    "time_weight": 60,
    "reservation": 0.4,
    "max_rounds": 6,
    "confirm_before_agreeing": True,
}


def b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def meeting_mandate() -> dict:
    return {
        "template": "meeting",
        "issues": [
            {
                "id": "time",
                "kind": "datetime",
                "choices": [
                    "2026-12-03T10:00:00+03:00",
                    "2026-12-03T12:00:00+03:00",
                    "2026-12-04T09:30:00+03:00",
                ],
            },
            {"id": "place", "kind": "place", "choices": ["кафе «Пушкин»", "офис"]},
        ],
        "utility": {
            "weights": {"time": 0.6, "place": 0.4},
            "value_functions": {
                "time": {"points": [[1796281200, 0.5], [1796288400, 1], [1796365800, 0]]},
                "place": {"points": [[0, 1], [1, 0]]},
            },
        },
        "hard_constraints": [
            {"kind": "cmp", "issue": "place", "op": "not_in", "value": ["вокзал"]}
        ],
        "reservation": {"minimum_utility": 0.4},
        "strategy": {"kind": "time_dependent", "params": {"beta": 0.5}},
        "limits": {"max_rounds": 6},
        "escalation": {"always": ["accept_deal"]},
    }


def vectors() -> dict:
    key = SigningKey.from_bytes(bytes(range(32)))
    message = "envoy/mandate/v1 · проверочный вектор".encode()
    mandate = meeting_mandate()
    Mandate.model_validate(mandate)
    return {
        "ed25519": {
            "seed": b64(bytes(range(32))),
            "public_key": b64(key.public().to_bytes()),
            "message": b64(message),
            "signature": b64(key.sign(message)),
        },
        "meeting": {"state": MEETING_STATE, "mandate": mandate},
    }


def schema() -> dict:
    return Mandate.model_json_schema()


def render(value: dict) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


if __name__ == "__main__":
    SCHEMA.parent.mkdir(parents=True, exist_ok=True)
    VECTORS.parent.mkdir(parents=True, exist_ok=True)
    SCHEMA.write_text(render(schema()), encoding="utf-8")
    VECTORS.write_text(render(vectors()), encoding="utf-8")
    print(SCHEMA)
    print(VECTORS)
