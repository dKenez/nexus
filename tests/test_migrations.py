from pathlib import Path

from alembic import command

from nexus.db.session import _alembic_config, upgrade_sync


def test_migrations_match_models(tmp_path: Path) -> None:
    url = f"sqlite+aiosqlite:///{tmp_path / 'm.db'}"
    upgrade_sync(url)
    # Raises if the models have changes that no migration covers.
    command.check(_alembic_config(url))
