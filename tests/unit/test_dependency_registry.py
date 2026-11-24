import json
from pathlib import Path

from envoy.telegram.dependencies import REGISTRY, missing_in


def test_registry_methods_exist_in_fixture() -> None:
    fixture = json.loads(Path("tests/fixtures/bot_api_methods.json").read_text())
    assert missing_in(set(fixture)) == []
    assert missing_in(set()) == sorted(dependency.method for dependency in REGISTRY)


def test_registry_has_no_duplicates() -> None:
    assert len({dependency.method for dependency in REGISTRY}) == len(REGISTRY)


def test_registry_includes_phase1_methods_in_fixture() -> None:
    fixture = set(json.loads(Path("tests/fixtures/bot_api_methods.json").read_text()))
    schema = json.loads(Path("tests/fixtures/business_schema.json").read_text())
    phase1 = {"sendChecklist", "createForumTopic", "answerCallbackQuery", "getMe"}
    registered = {dependency.method for dependency in REGISTRY}
    assert phase1 <= registered
    assert phase1 <= fixture
    for method in ("sendChecklist", "createForumTopic", "answerCallbackQuery"):
        assert method in schema["methods"]
