from nexus.db.models import GameState, GameStatus, HostStatus
from nexus.infra.agent import ContainerInfo
from nexus.infra.hetzner import ServerInfo
from tests.conftest import World


async def test_idle_game_is_stopped_after_idle_minutes(world: World) -> None:
    await world.orch.start("alpha")
    world.query.players["alpha"] = 0

    world.clock.advance(minutes=19)
    assert await world.orch.poll_players() == []

    world.clock.advance(minutes=2)
    assert await world.orch.poll_players() == ["alpha"]
    assert (await world.orch.game("alpha")).status is GameStatus.STOPPED
    assert world.hetzner.servers == {}
    assert len(await world.orch.backups("alpha")) == 1


async def test_players_reset_the_idle_timer(world: World) -> None:
    await world.orch.start("alpha")
    world.query.players["alpha"] = 2
    world.clock.advance(minutes=15)
    await world.orch.poll_players()

    world.query.players["alpha"] = 0
    world.clock.advance(minutes=15)
    assert await world.orch.poll_players() == []
    assert (await world.orch.game("alpha")).players == 0

    world.clock.advance(minutes=6)
    assert await world.orch.poll_players() == ["alpha"]


async def test_startup_grace_protects_new_games(world: World) -> None:
    orch = world.orch
    orch.recipes._recipes["alpha"] = orch.recipes.get("alpha").model_copy(
        update={"idle_minutes": 1, "startup_grace_minutes": 10}
    )
    await orch.start("alpha")
    world.clock.advance(minutes=5)
    assert await orch.poll_players() == []
    world.clock.advance(minutes=6)
    assert await orch.poll_players() == ["alpha"]


async def test_unreachable_server_counts_as_empty(world: World) -> None:
    await world.orch.start("alpha")
    world.query.players["alpha"] = None
    world.clock.advance(minutes=21)
    assert await world.orch.poll_players() == ["alpha"]


async def test_vanished_host_is_detected(world: World) -> None:
    await world.orch.start("alpha")
    world.hetzner.servers.clear()

    await world.orch.reconcile_hosts()

    assert await world.orch.host() is None
    view = await world.orch.game("alpha")
    assert view.status is GameStatus.STOPPED
    assert any("lost" in msg for _, msg in world.notifier.messages)


async def test_untracked_server_is_adopted(world: World) -> None:
    world.hetzner.servers[555] = ServerInfo(
        id=555,
        name="nexus-test-old",
        status="running",
        ip="203.0.113.10",
        server_type="cx32",
        labels={"managed-by": "nexus", "nexus-env": "test"},
    )
    await world.orch.reconcile_hosts()
    host = await world.orch.host()
    assert host is not None
    assert host.hcloud_id == 555
    assert host.status is HostStatus.READY


async def test_recover_marks_running_container_running(world: World) -> None:
    await world.orch.start("alpha")
    async with world.sessions() as s, s.begin():
        (await s.get_one(GameState, "alpha")).status = GameStatus.STARTING

    await world.orch.recover()
    assert (await world.orch.game("alpha")).status is GameStatus.RUNNING
    # The start's progress message died with the old process; players are told here instead.
    assert any("Alpha is up at" in m for _, m in world.notifier.messages)


async def test_recover_finishes_interrupted_stop(world: World) -> None:
    await world.orch.start("alpha")
    async with world.sessions() as s, s.begin():
        (await s.get_one(GameState, "alpha")).status = GameStatus.BACKING_UP
    world.agents.agent.containers_["alpha"] = ContainerInfo(
        game="alpha", name="nexus-alpha", running=False, status="Exited"
    )

    await world.orch.recover()
    assert (await world.orch.game("alpha")).status is GameStatus.STOPPED
    assert len(await world.orch.backups("alpha")) == 1
    assert world.hetzner.servers == {}


async def test_interrupted_provisioning_is_cleaned_up(world: World) -> None:
    # Crash between "create server" and "save its id": a PROVISIONING row without hcloud_id,
    # and a real server holding the Primary IP.
    from nexus.db.models import Host

    async with world.sessions() as s, s.begin():
        s.add(
            Host(
                name="nexus-test-x",
                status=HostStatus.PROVISIONING,
                server_type="cx32",
                memory_mb=8192,
                created_at=world.clock(),
            )
        )
    world.hetzner.servers[777] = ServerInfo(
        id=777,
        name="nexus-test-x",
        status="running",
        ip="203.0.113.10",
        server_type="cx32",
        labels={"managed-by": "nexus", "nexus-env": "test"},
    )

    await world.orch.reconcile_hosts()
    host = await world.orch.host()
    assert host is not None and host.hcloud_id == 777 and host.status is HostStatus.READY

    # The adopted host is usable: starting a game reuses it instead of fighting for the IP.
    await world.orch.start("alpha")
    assert world.hetzner.created == 0


async def test_untracked_running_container_is_adopted_not_deleted(world: World) -> None:
    world.hetzner.servers[555] = ServerInfo(
        id=555,
        name="nexus-test-old",
        status="running",
        ip="203.0.113.10",
        server_type="cx32",
        labels={"managed-by": "nexus", "nexus-env": "test"},
    )
    world.agents.agent.data["alpha"] = b"precious"
    world.agents.agent.containers_["alpha"] = ContainerInfo(
        game="alpha", name="nexus-alpha", running=True, status="Up"
    )

    await world.orch.recover()
    view = await world.orch.game("alpha")
    assert view.status is GameStatus.RUNNING
    assert view.dirty

    # Host GC must not delete it; the idle logic stops it with a backup instead.
    world.clock.advance(hours=1)
    assert not await world.orch.gc_host()
    assert await world.orch.poll_players() == ["alpha"]
    assert world.hetzner.servers == {}
    backups = await world.orch.backups("alpha")
    assert (
        b"".join([c async for c in world.backups.read("alpha", backups[0].filename)]) == b"precious"
    )
