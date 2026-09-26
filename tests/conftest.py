import os
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from nexus.config import Settings
from nexus.core.orchestrator import Orchestrator, OrchestratorConfig
from nexus.core.recipes import Recipe, RecipeBook
from nexus.db.models import Base
from nexus.db.session import make_engine, make_sessionmaker
from nexus.infra.backups import BackupStore
from tests.fakes import FakeAgentFactory, FakeClock, FakeHetzner, FakeQuery, RecordingNotifier


def recipe(name: str, memory_mb: int = 2048, port: int = 2456, **extra: object) -> Recipe:
    return Recipe.model_validate(
        {
            "name": name,
            "display_name": name.title(),
            "image": f"ghcr.io/test/{name}",
            "version": "1",
            "memory_mb": memory_mb,
            "ports": [{"port": port, "protocol": "udp"}, {"port": port + 1, "protocol": "udp"}],
            "query": {"type": "a2s", "port": port + 1},
            "idle_minutes": 20,
            "startup_grace_minutes": 5,
            "secret_env": ["SERVER_PASSWORD"],
            **extra,
        }
    )


RECIPES = {
    "alpha": recipe("alpha", port=2456),
    "beta": recipe("beta", port=3456),
    "huge": recipe("huge", memory_mb=6000, port=4456),
}
ENVIRON = {f"NEXUS_GAME_{name.upper()}_SERVER_PASSWORD": "hunter22" for name in RECIPES}


@dataclass
class World:
    orch: Orchestrator
    hetzner: FakeHetzner
    agents: FakeAgentFactory
    notifier: RecordingNotifier
    clock: FakeClock
    query: FakeQuery
    backups: BackupStore
    sessions: async_sessionmaker[AsyncSession]


@pytest.fixture(autouse=True)
def _isolated_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Hide the developer's real configuration from tests.

    mise loads .env into the environment, and Settings reads the environment, so without this
    a test could pick up the real Discord token (and log the bot in) or real Hetzner settings.
    NEXUS_TEST_DATABASE_URL is not a setting and is kept.
    """
    names = {name.upper() for name in Settings.model_fields}
    for key in list(os.environ):
        if key in names or key.startswith("NEXUS_GAME_"):
            monkeypatch.delenv(key)


@pytest.fixture
async def sessions(tmp_path: Path) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    # NEXUS_TEST_DATABASE_URL runs the suite against a real (throwaway!) Postgres.
    url = os.environ.get("NEXUS_TEST_DATABASE_URL") or f"sqlite+aiosqlite:///{tmp_path / 'n.db'}"
    engine = make_engine(url)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    yield make_sessionmaker(engine)
    await engine.dispose()


@pytest.fixture
def world(tmp_path: Path, sessions: async_sessionmaker[AsyncSession]) -> World:
    hetzner = FakeHetzner()
    agents = FakeAgentFactory()
    notifier = RecordingNotifier()
    clock = FakeClock()
    query = FakeQuery()
    backups = BackupStore(tmp_path / "backups")
    orch = Orchestrator(
        config=OrchestratorConfig(
            env="test",
            server_type="cx32",
            host_memory_reserve_mb=1024,
            backup_retention=3,
            host_idle_grace=600,
        ),
        recipes=RecipeBook(dict(RECIPES)),
        sessions=sessions,
        hetzner=hetzner,
        agents=agents,
        backups=backups,
        notifier=notifier,
        user_data=lambda: "#cloud-config\n",
        ssh_public_key="ssh-ed25519 AAAA test",
        query=query,
        clock=clock,
        environ=ENVIRON,
        poll_interval=0,
    )
    return World(orch, hetzner, agents, notifier, clock, query, backups, sessions)
