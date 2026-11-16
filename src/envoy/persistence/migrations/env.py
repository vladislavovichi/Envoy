import asyncio

from alembic import context
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from envoy.app.settings import Settings
from envoy.persistence.models import Base

config = context.config


def migrate(connection: Connection) -> None:
    context.configure(connection=connection, target_metadata=Base.metadata, compare_type=True)
    with context.begin_transaction():
        context.run_migrations()


def database_url() -> str:
    if "database_url" in config.attributes:
        return str(config.attributes["database_url"])
    settings = Settings()
    if settings.database_url is None:
        raise ValueError("ENVOY_DATABASE_URL is required for migrations")
    return settings.database_url.get_secret_value()


async def migrate_async() -> None:
    engine = create_async_engine(database_url(), poolclass=NullPool)
    try:
        async with engine.connect() as connection:
            await connection.run_sync(migrate)
    finally:
        await engine.dispose()


if context.is_offline_mode():
    context.configure(url=database_url(), target_metadata=Base.metadata, literal_binds=True)
    with context.begin_transaction():
        context.run_migrations()
elif "connection" in config.attributes:
    migrate(config.attributes["connection"])
else:
    asyncio.run(migrate_async())
