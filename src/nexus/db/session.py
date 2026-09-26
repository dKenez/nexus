"""Database engine, sessions and startup migrations."""

import asyncio
from pathlib import Path

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

MIGRATIONS_DIR = Path(__file__).parent / "migrations"


def make_engine(url: str) -> AsyncEngine:
    return create_async_engine(url, pool_pre_ping=True)


def make_sessionmaker(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False)


def _alembic_config(url: str):
    from alembic.config import Config

    config = Config()
    config.set_main_option("script_location", str(MIGRATIONS_DIR))
    config.set_main_option("sqlalchemy.url", url)
    return config


def upgrade_sync(url: str) -> None:
    from alembic import command

    command.upgrade(_alembic_config(url), "head")


async def upgrade(url: str) -> None:
    """Apply all migrations. Alembic's env runs its own event loop, so do it in a thread."""
    await asyncio.to_thread(upgrade_sync, url)
