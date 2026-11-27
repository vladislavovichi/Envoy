import importlib.util
import json
from pathlib import Path

from envoy.mandate.schema import Mandate

SCHEMA = Path("miniapp/src/generated/mandate.schema.json")
VECTORS = Path("miniapp/tests/fixtures/vectors.json")


def generator():
    spec = importlib.util.spec_from_file_location("miniapp_vectors", "scripts/miniapp_vectors.py")
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_generated_schema_and_vectors_are_fresh() -> None:
    module = generator()
    assert SCHEMA.read_text("utf-8") == module.render(module.schema())
    assert VECTORS.read_text("utf-8") == module.render(module.vectors())


def test_meeting_vector_is_a_valid_server_mandate() -> None:
    vector = json.loads(VECTORS.read_text("utf-8"))["meeting"]["mandate"]
    assert Mandate.model_validate(vector).template == "meeting"
    assert json.loads(SCHEMA.read_text("utf-8")) == Mandate.model_json_schema()
