import pytest

from tests.db_safety import guard_test_database


@pytest.mark.parametrize(
    "test_url",
    [
        "postgresql+asyncpg://different@localhost/db",
        "postgresql://localhost:5432/db",
        "postgresql+asyncpg://127.0.0.1:5432/db",
    ],
)
def test_database_fixtures_reject_application_database(test_url: str) -> None:
    with pytest.raises(ValueError, match="separate"):
        guard_test_database(test_url, "postgresql+asyncpg://localhost/db")


def test_database_fixtures_accept_isolated_instance() -> None:
    guard_test_database("postgresql+asyncpg://postgres-test/db", "postgresql+asyncpg://postgres/db")
