import asyncio

import httpx
import pytest

from nexus.api.app import build_api
from nexus.core.tasks import TaskRunner
from nexus.db.models import GameStatus
from tests.conftest import World

TOKEN = "t" * 32


@pytest.fixture
def client(world: World) -> httpx.AsyncClient:
    api = build_api()
    api.state.api_token = TOKEN
    api.state.orchestrator = world.orch
    api.state.sessions = world.sessions
    api.state.tasks = TaskRunner(world.notifier)
    transport = httpx.ASGITransport(app=api)
    return httpx.AsyncClient(
        transport=transport, base_url="http://test", headers={"X-API-KEY": TOKEN}
    )


async def _settle() -> None:
    for _ in range(50):
        await asyncio.sleep(0)
        tasks = [t for t in asyncio.all_tasks() if t.get_name().startswith(("start", "stop"))]
        if not tasks:
            return
        await asyncio.gather(*tasks)


async def test_requires_api_key(client: httpx.AsyncClient) -> None:
    for headers in ({"X-API-KEY": ""}, {"X-API-KEY": "wrong"}):
        response = await client.get("/api/games", headers=headers)
        assert response.status_code == 403


async def test_health_endpoints_are_open(client: httpx.AsyncClient) -> None:
    client.headers.pop("X-API-KEY")
    assert (await client.get("/healthz")).status_code == 200
    assert (await client.get("/readyz")).json() == {"status": "ok"}


async def test_list_games(client: httpx.AsyncClient) -> None:
    games = (await client.get("/api/games")).json()
    assert {g["name"] for g in games} == {"alpha", "beta", "huge"}
    assert all(g["status"] == "stopped" for g in games)


async def test_unknown_game_is_404(client: httpx.AsyncClient) -> None:
    assert (await client.get("/api/games/nope")).status_code == 404


async def test_start_and_stop_in_background(client: httpx.AsyncClient, world: World) -> None:
    response = await client.post("/api/games/alpha/start")
    assert response.status_code == 202
    await _settle()
    assert (await world.orch.game("alpha")).status is GameStatus.RUNNING

    host = (await client.get("/api/host")).json()
    assert host["status"] == "ready"
    assert host["hourly_price"] == "0.0120"

    assert (await client.post("/api/games/alpha/start")).status_code == 409

    assert (await client.post("/api/games/alpha/stop")).status_code == 202
    await _settle()
    assert (await world.orch.game("alpha")).status is GameStatus.STOPPED
    backups = (await client.get("/api/games/alpha/backups")).json()
    assert len(backups) == 1
    assert (await client.get("/api/host")).json() is None


async def test_restore_pins_backup(client: httpx.AsyncClient, world: World) -> None:
    await world.orch.start("alpha")
    await world.orch.stop("alpha")
    backup = (await world.orch.backups("alpha"))[0]

    response = await client.post("/api/games/alpha/restore", params={"backup_id": backup.id})
    assert response.status_code == 200
    assert (await world.orch.game("alpha")).pinned_backup_id == backup.id
    missing = await client.post("/api/games/alpha/restore", params={"backup_id": 999})
    assert missing.status_code == 409


async def test_destroy_needs_force(client: httpx.AsyncClient) -> None:
    assert (await client.post("/api/host/destroy")).status_code == 409
    response = await client.post("/api/host/destroy", params={"force": True})
    assert response.json()["accepted"] is False


async def test_list_snapshots(client: httpx.AsyncClient) -> None:
    response = await client.get("/api/games/alpha/snapshots")
    assert response.status_code == 200
    assert response.json() == []


async def test_duplicate_stop_is_rejected_not_queued(
    client: httpx.AsyncClient, world: World
) -> None:
    await world.orch.start("alpha")
    first, second = await asyncio.gather(
        client.post("/api/games/alpha/stop"), client.post("/api/games/alpha/stop")
    )
    assert sorted([first.status_code, second.status_code]) == [202, 409]
    rejected = first if first.status_code == 409 else second
    assert rejected.json()["detail"] == "Alpha is already stopping"
    await _settle()
    assert (await world.orch.game("alpha")).status is GameStatus.STOPPED
    # No failure was reported for the duplicate.
    assert not any("failed" in m for _, m in world.notifier.messages)
    # And the reservation is released: the game can be started again.
    assert (await client.post("/api/games/alpha/start")).status_code == 202
    await _settle()


async def test_rejected_request_releases_reservation(client: httpx.AsyncClient) -> None:
    assert (await client.post("/api/games/alpha/stop")).status_code == 409  # not running
    assert (await client.post("/api/games/alpha/stop")).status_code == 409
    assert (await client.post("/api/games/alpha/start")).status_code == 202
    await _settle()


async def test_duplicate_shutdown_is_rejected(client: httpx.AsyncClient, world: World) -> None:
    await world.orch.start("alpha")
    first, second = await asyncio.gather(
        client.post("/api/host/shutdown"), client.post("/api/host/shutdown")
    )
    assert sorted([first.status_code, second.status_code]) == [202, 409]
    rejected = first if first.status_code == 409 else second
    assert rejected.json()["detail"] == "the host is already shutting down"
    await asyncio.gather(*[t for t in asyncio.all_tasks() if t.get_name() == "host shutdown"])
    assert world.hetzner.servers == {}
    assert len(await world.orch.backups("alpha")) == 1
    assert not any("failed" in m for _, m in world.notifier.messages)
    assert world.orch.operation("*host*") is None


async def test_game_requests_are_rejected_during_shutdown(
    client: httpx.AsyncClient, world: World
) -> None:
    await world.orch.start("alpha")
    op = world.orch.begin_host("shutting down")
    for path in ("/api/games/beta/start", "/api/games/alpha/stop", "/api/games/alpha/backup"):
        response = await client.post(path)
        assert response.status_code == 409, path
        assert "shutting down" in response.json()["detail"]
    op.release()
    assert world.hetzner.created == 1


async def test_destroy_is_not_blocked_by_a_running_shutdown(
    client: httpx.AsyncClient, world: World
) -> None:
    await world.orch.start("alpha")
    op = world.orch.begin_host("shutting down")  # a shutdown that got stuck
    response = await client.post("/api/host/destroy", params={"force": True})
    assert response.json()["accepted"] is True
    op.release()


async def test_restore_is_rejected_while_game_is_busy(
    client: httpx.AsyncClient, world: World
) -> None:
    await world.orch.start("alpha")
    await world.orch.stop("alpha")
    backup = (await world.orch.backups("alpha"))[0]
    op = world.orch.begin("alpha", "starting")
    response = await client.post("/api/games/alpha/restore", params={"backup_id": backup.id})
    assert response.status_code == 409
    assert response.json()["detail"] == "Alpha is already starting"
    op.release()
    ok = await client.post("/api/games/alpha/restore", params={"backup_id": backup.id})
    assert ok.status_code == 200


async def test_duplicate_start_is_rejected(client: httpx.AsyncClient, world: World) -> None:
    first, second = await asyncio.gather(
        client.post("/api/games/alpha/start"), client.post("/api/games/alpha/start")
    )
    assert sorted([first.status_code, second.status_code]) == [202, 409]
    await _settle()
    assert world.hetzner.created == 1
    assert not any("failed" in m for _, m in world.notifier.messages)
