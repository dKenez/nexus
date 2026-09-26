"""The real process wiring (migrations, recipes, reconciler) with only Hetzner faked."""

import asyncio
from pathlib import Path

import asyncssh
import httpx
import pytest
from pydantic import SecretStr

from nexus import app as app_module
from nexus.config import Settings
from tests.fakes import FakeHetzner

REPO = Path(__file__).parent.parent


def _key() -> SecretStr:
    return SecretStr(asyncssh.generate_private_key("ssh-ed25519").export_private_key().decode())


async def test_app_starts_and_serves(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    hetzner = FakeHetzner()
    monkeypatch.setattr(app_module, "build_gateway", lambda settings: hetzner)
    settings = Settings(
        nexus_api_token=SecretStr("x" * 32),
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'app.db'}",
        recipes_dir=REPO / "recipes",
        backup_dir=tmp_path / "backups",
        hcloud_token=SecretStr("unused"),
        hcloud_primary_ip="nexus-test",
        ssh_client_key=_key(),
        ssh_host_key=_key(),
        reconcile_interval=5,
    )
    app = app_module.create_app(settings)

    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
            assert (await client.get("/readyz")).json() == {"status": "ok"}
            games = await client.get("/api/games", headers={"X-API-KEY": "x" * 32})
            assert [g["name"] for g in games.json()] == ["valheim"]
        await asyncio.sleep(0.05)
        assert app.state.reconciler.healthy

    assert (tmp_path / "backups").is_dir()
