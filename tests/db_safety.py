from sqlalchemy.engine import make_url
from sqlalchemy.exc import ArgumentError


def guard_test_database(test_url: str, application_url: str | None) -> None:
    if application_url is None:
        return
    try:
        test, application = make_url(test_url), make_url(application_url)
    except ArgumentError:
        raise ValueError("Valid PostgreSQL database URLs are required") from None

    def identity(url):
        host = (url.host or "localhost").lower()
        if host in {"127.0.0.1", "::1"}:
            host = "localhost"
        return host, url.port or 5432, url.database

    if identity(test) == identity(application):
        raise ValueError("Tests require a separate database or PostgreSQL instance")
