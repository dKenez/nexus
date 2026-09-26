import pytest

from nexus.core.notify import Level
from nexus.core.orchestrator import CapacityError, InvalidStateError, NexusError
from nexus.core.recipes import MissingSecretError, Port
from nexus.db.models import GameStatus, HostStatus
from tests.conftest import World


async def test_start_provisions_host_and_runs_game(world: World) -> None:
    view = await world.orch.start("alpha")

    assert view.status is GameStatus.RUNNING
    assert view.dirty
    assert world.hetzner.created == 1
    assert world.agents.agent.containers_["alpha"].running
    assert world.agents.agent.env["alpha"]["SERVER_PASSWORD"] == "hunter22"
    assert world.hetzner.firewall == [
        Port(port=2456, protocol="udp"),
        Port(port=2457, protocol="udp"),
    ]
    host = await world.orch.host()
    assert host is not None and host.status is HostStatus.READY


async def test_second_game_joins_existing_host(world: World) -> None:
    await world.orch.start("alpha")
    await world.orch.start("beta")

    assert world.hetzner.created == 1
    assert {p.port for p in world.hetzner.firewall} == {2456, 2457, 3456, 3457}


async def test_capacity_is_enforced(world: World) -> None:
    await world.orch.start("alpha")
    with pytest.raises(CapacityError):
        await world.orch.start("huge")
    assert (await world.orch.game("huge")).status is GameStatus.STOPPED
    # The failed claim didn't disturb the running game or its firewall.
    assert {p.port for p in world.hetzner.firewall} == {2456, 2457}


async def test_start_twice_is_rejected(world: World) -> None:
    await world.orch.start("alpha")
    with pytest.raises(InvalidStateError):
        await world.orch.start("alpha")


async def test_missing_secret_fails_before_touching_hetzner(world: World) -> None:
    world.orch._environ = {}
    with pytest.raises(MissingSecretError):
        await world.orch.start("alpha")
    assert world.hetzner.created == 0


async def test_stop_backs_up_and_deletes_empty_host(world: World) -> None:
    await world.orch.start("alpha")
    view = await world.orch.stop("alpha")

    assert view.status is GameStatus.STOPPED
    assert not view.dirty
    assert world.hetzner.servers == {}
    backups = await world.orch.backups("alpha")
    assert len(backups) == 1
    content = b"".join([c async for c in world.backups.read("alpha", backups[0].filename)])
    assert content == b"+played"
    assert await world.orch.host() is None


async def test_stop_keeps_host_while_other_game_runs(world: World) -> None:
    await world.orch.start("alpha")
    await world.orch.start("beta")
    await world.orch.stop("alpha")

    assert len(world.hetzner.servers) == 1
    assert {p.port for p in world.hetzner.firewall} == {3456, 3457}


async def test_restart_restores_latest_backup(world: World) -> None:
    await world.orch.start("alpha")
    await world.orch.stop("alpha")
    world.clock.advance(minutes=5)
    await world.orch.start("alpha")

    # Fresh VM, world restored from the backup, then played on.
    assert world.agents.agent.data["alpha"] == b"+played+played"
    assert world.hetzner.created == 2


async def test_backup_failure_keeps_host(world: World) -> None:
    await world.orch.start("alpha")
    world.agents.agent.fail_archive = True

    with pytest.raises(NexusError):
        await world.orch.stop("alpha")

    view = await world.orch.game("alpha")
    assert view.status is GameStatus.FAILED
    assert view.dirty
    assert len(world.hetzner.servers) == 1
    assert any(level is Level.ERROR for level, _ in world.notifier.messages)
    # Even the empty-host GC must not delete it.
    world.clock.advance(hours=2)
    assert not await world.orch.gc_host()
    assert len(world.hetzner.servers) == 1

    # Once the problem is fixed, a stop recovers the data and releases the host.
    world.agents.agent.fail_archive = False
    await world.orch.stop("alpha")
    assert world.hetzner.servers == {}
    assert len(await world.orch.backups("alpha")) == 1


async def test_failed_provisioning_leaves_nothing_behind(world: World) -> None:
    world.agents.fail_ready = True
    with pytest.raises(NexusError):
        await world.orch.start("alpha")

    assert world.hetzner.servers == {}
    view = await world.orch.game("alpha")
    assert view.status is GameStatus.STOPPED
    assert view.last_error
    # And it can be retried.
    world.agents.fail_ready = False
    assert (await world.orch.start("alpha")).status is GameStatus.RUNNING


async def test_destroy_host_forces_deletion(world: World) -> None:
    await world.orch.start("alpha")
    world.agents.agent.fail_archive = True
    with pytest.raises(NexusError):
        await world.orch.stop("alpha")

    assert await world.orch.destroy_host()
    assert world.hetzner.servers == {}
    view = await world.orch.game("alpha")
    assert view.status is GameStatus.STOPPED
    assert not view.dirty


async def test_shutdown_host_stops_everything(world: World) -> None:
    await world.orch.start("alpha")
    await world.orch.start("beta")
    await world.orch.shutdown_host()

    assert world.hetzner.servers == {}
    assert len(await world.orch.backups("alpha")) == 1
    assert len(await world.orch.backups("beta")) == 1


async def test_retention_prunes_old_backups(world: World) -> None:
    for _ in range(5):
        await world.orch.start("alpha")
        world.clock.advance(minutes=1)
        await world.orch.stop("alpha")
        world.clock.advance(minutes=1)

    backups = await world.orch.backups("alpha")
    assert len(backups) == 3
    files = sorted(p.name for p in world.backups.game_dir("alpha").iterdir())
    assert files == sorted(b.filename for b in backups)


async def test_pinned_backup_is_restored(world: World) -> None:
    await world.orch.start("alpha")
    await world.orch.stop("alpha")
    first = (await world.orch.backups("alpha"))[0]
    world.clock.advance(minutes=1)
    await world.orch.start("alpha")
    await world.orch.stop("alpha")

    await world.orch.pin_backup("alpha", first.id)
    await world.orch.start("alpha")
    assert world.agents.agent.data["alpha"] == b"+played+played"


async def test_pin_requires_stopped_game(world: World) -> None:
    await world.orch.start("alpha")
    await world.orch.stop("alpha")
    backup = (await world.orch.backups("alpha"))[0]
    await world.orch.start("alpha")
    with pytest.raises(InvalidStateError):
        await world.orch.pin_backup("alpha", backup.id)


async def test_hot_backup_keeps_game_running(world: World) -> None:
    await world.orch.start("alpha")
    await world.orch.backup("alpha")

    view = await world.orch.game("alpha")
    assert view.status is GameStatus.RUNNING
    assert world.agents.agent.containers_["alpha"].running
    assert len(await world.orch.backups("alpha")) == 1


async def test_concurrent_starts_one_wins(world: World) -> None:
    import asyncio

    from nexus.core.orchestrator import BusyError

    results = await asyncio.gather(
        world.orch.start("alpha"), world.orch.start("alpha"), return_exceptions=True
    )
    assert sum(isinstance(r, BusyError) for r in results) == 1
    assert world.orch.operation("alpha") is None
    assert world.hetzner.created == 1


async def test_idle_stop_skips_busy_game(world: World) -> None:
    await world.orch.start("alpha")
    op = world.orch.begin("alpha", "being backed up")
    world.clock.advance(minutes=30)
    assert await world.orch.poll_players() == []  # busy: left alone, no error
    op.release()
    assert await world.orch.poll_players() == ["alpha"]


async def test_concurrent_starts_of_different_games_share_one_vm(world: World) -> None:
    import asyncio

    alpha, beta = await asyncio.gather(world.orch.start("alpha"), world.orch.start("beta"))
    assert alpha.status is GameStatus.RUNNING
    assert beta.status is GameStatus.RUNNING
    assert world.hetzner.created == 1
    assert {p.port for p in world.hetzner.firewall} == {2456, 2457, 3456, 3457}


async def test_stop_during_start_is_rejected(world: World) -> None:
    from nexus.core.orchestrator import BusyError

    op = world.orch.begin("alpha", "starting")
    with pytest.raises(BusyError, match="already starting"):
        await world.orch.stop("alpha")
    op.release()
