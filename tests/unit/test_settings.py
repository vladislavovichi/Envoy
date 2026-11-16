import subprocess
from pathlib import Path

import pytest
from pydantic import ValidationError

from envoy.app.settings import Settings


def test_missing_mandatory_secrets_fail_clearly(monkeypatch) -> None:
    monkeypatch.delenv("ENVOY_AGENT_KEY", raising=False)
    monkeypatch.delenv("ENVOY_MASTER_KEY", raising=False)
    with pytest.raises(ValidationError) as error:
        Settings(_env_file=None)
    assert {item["loc"][0] for item in error.value.errors()} >= {"agent_key", "master_key"}


def test_settings_snapshot(snapshot) -> None:
    assert sorted(Settings.model_fields) == snapshot


def test_env_file_is_gitignored(tmp_path) -> None:
    subprocess.run(["git", "init", "--quiet", str(tmp_path)], check=True)
    (tmp_path / ".gitignore").write_bytes(Path(".gitignore").read_bytes())
    result = subprocess.run(
        ["git", "check-ignore", ".env"], cwd=tmp_path, capture_output=True, check=False
    )
    assert result.returncode == 0


def test_env_example_keys_match_settings() -> None:
    keys = {
        line.split("=", 1)[0]
        for line in Path(".env.example").read_text().splitlines()
        if "=" in line
    }
    assert keys == {"ENVOY_" + field.upper() for field in Settings.model_fields}
    assert all(
        not line.split("=", 1)[1]
        for line in Path(".env.example").read_text().splitlines()
        if "=" in line
    )
