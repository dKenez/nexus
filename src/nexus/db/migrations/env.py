import asyncio
import os
from typing import Literal

from alembic import context
from sqlalchemy import Connection

from nexus.db.models import Base, UTCDateTime
from nexus.db.session import make_engine

config = context.config
target_metadata = Base.metadata


def _url() -> str:
    # The app passes its URL in; the CLI (`mise run db:*`) only needs DATABASE_URL, not the
    # whole app configuration.
    url = config.get_main_option("sqlalchemy.url") or os.environ.get("DATABASE_URL")
    if not url:
        raise RuntimeError("DATABASE_URL is not set")
    return url


def render_item(type_: str, obj: object, autogen_context: object) -> str | Literal[False]:
    # UTCDateTime only changes Python-side handling; the column is a plain timestamptz.
    if type_ == "type" and isinstance(obj, UTCDateTime):
        return "sa.DateTime(timezone=True)"
    return False


def run_migrations_offline() -> None:
    context.configure(
        url=_url(), target_metadata=target_metadata, literal_binds=True, render_item=render_item
    )
    with context.begin_transaction():
        context.run_migrations()


def _run(connection: Connection) -> None:
    context.configure(
        connection=connection, target_metadata=target_metadata, render_item=render_item
    )
    with context.begin_transaction():
        context.run_migrations()


async def run_migrations_online() -> None:
    engine = make_engine(_url())
    async with engine.connect() as connection:
        await connection.run_sync(_run)
    await engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    asyncio.run(run_migrations_online())
