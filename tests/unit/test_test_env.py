import subprocess
import sys

from dotenv import dotenv_values

from envoy.app.settings import Settings


def test_test_environment_is_valid_and_does_not_overwrite_secrets(tmp_path) -> None:
    destination = tmp_path / ".env"
    command = [sys.executable, "scripts/create_test_env.py", str(destination)]
    subprocess.run(command, check=True)
    values = dotenv_values(destination)
    settings = Settings(_env_file=destination)
    assert settings.journal_store == "postgres"
    assert len(bytes.fromhex(settings.master_key.get_secret_value())) == 32
    assert values["ENVOY_POSTGRES_PASSWORD"]
    assert values["ENVOY_DATABASE_URL"] != values["ENVOY_TEST_DATABASE_URL"]
    previous = destination.read_bytes()
    assert subprocess.run(command, capture_output=True, check=False).returncode != 0
    assert destination.read_bytes() == previous
