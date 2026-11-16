import secrets
import sys
from pathlib import Path

destination = Path(sys.argv[1] if len(sys.argv) > 1 else ".env")
password = secrets.token_hex(32)
database_url = f"postgresql+asyncpg://envoy:{password}@postgres:5432/envoy_phase0"
values = {
    "ENVOY_JOURNAL_STORE": "postgres",
    "ENVOY_TELEGRAM_TRANSPORT": "fake",
    "ENVOY_DATABASE_URL": database_url,
    "ENVOY_BOT_TOKEN": "",
    "ENVOY_AGENT_KEY": secrets.token_hex(32),
    "ENVOY_MASTER_KEY": secrets.token_hex(32),
    "ENVOY_GLOBAL_RATE": "30",
    "ENVOY_PER_CHAT_RATE": "1",
    "ENVOY_MAX_RETRY_AFTER": "60",
    "ENVOY_TELEGRAM_API_BASE": "https://api.telegram.org",
    "ENVOY_POSTGRES_USER": "envoy",
    "ENVOY_POSTGRES_PASSWORD": password,
    "ENVOY_POSTGRES_DB": "envoy_phase0",
    "ENVOY_POSTGRES_PORT": "55432",
    "ENVOY_TEST_DATABASE_URL": database_url.replace("@postgres:", "@postgres-test:"),
}
with destination.open("x", encoding="utf-8") as output:
    output.write("".join(f"{key}={value}\n" for key, value in values.items()))
