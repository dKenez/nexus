"""Hetzner bills a server per started hour of its life: never pay twice for the same hour."""

import dataclasses
from datetime import timedelta

from nexus.core.orchestrator import paid_until
from nexus.db.models import GameState, GameStatus
from tests.conftest import World


async def created_at(world: World):
    host = await world.orch.host()
    assert host is not None
    return host.created_at


def bill_like_hetzner(world: World) -> None:
    world.orch.config = dataclasses.replace(world.orch.config, host_billing_margin=300)


def test_paid_until() -> None:
    from datetime import UTC, datetime

    t0 = datetime(2026, 9, 26, 12, 0, tzinfo=UTC)
    assert paid_until(t0, t0) == t0 + timedelta(hours=1)
    assert paid_until(t0, t0 + timedelta(minutes=59)) == t0 + timedelta(hours=1)
    assert paid_until(t0, t0 + timedelta(minutes=61)) == t0 + timedelta(hours=2)


async def test_empty_host_is_kept_for_its_paid_hour(world: World) -> None:
    bill_like_hetzner(world)
    await world.orch.start("alpha")
    created = await created_at(world)
    world.clock.advance(minutes=10)
    await world.orch.stop("alpha")

    host = await world.orch.host()
    assert host is not None, "the hour is paid for; keep the VM"
    assert host.delete_at == created + timedelta(minutes=55)
    assert len(await world.orch.backups("alpha")) == 1  # the world is safe regardless

    world.clock.advance(minutes=44)  # minute 54
    assert not await world.orch.gc_host()
    world.clock.advance(minutes=2)  # minute 56
    assert await world.orch.gc_host()
    assert world.hetzner.servers == {}


async def test_restart_within_the_paid_hour_reuses_the_vm(world: World) -> None:
    bill_like_hetzner(world)
    await world.orch.start("alpha")
    await world.orch.stop("alpha")
    world.clock.advance(minutes=20)
    await world.orch.start("alpha")

    assert world.hetzner.created == 1
    host = await world.orch.host()
    assert host is not None and host.delete_at is None  # busy again: not scheduled
    # The world came back from the stop's backup onto the same VM.
    assert world.agents.agent.data["alpha"] == b"+played+played"


async def test_stop_near_the_end_of_the_hour_deletes_right_away(world: World) -> None:
    bill_like_hetzner(world)
    await world.orch.start("alpha")
    world.clock.advance(minutes=57)
    await world.orch.stop("alpha")
    assert world.hetzner.servers == {}


async def test_long_session_is_kept_until_its_last_paid_hour_ends(world: World) -> None:
    bill_like_hetzner(world)
    await world.orch.start("alpha")
    created = await created_at(world)
    world.clock.advance(hours=2, minutes=10)
    await world.orch.stop("alpha")
    host = await world.orch.host()
    assert host is not None
    assert host.delete_at == created + timedelta(hours=2, minutes=55)


async def test_explicit_shutdown_deletes_immediately(world: World) -> None:
    bill_like_hetzner(world)
    await world.orch.start("alpha")
    await world.orch.shutdown_host()
    assert world.hetzner.servers == {}


async def test_gc_catches_an_empty_host_left_by_drift(world: World) -> None:
    bill_like_hetzner(world)
    await world.orch.start("alpha")
    async with world.sessions() as s, s.begin():
        state = await s.get_one(GameState, "alpha")
        state.status = GameStatus.STOPPED
        state.dirty = False
    assert not await world.orch.gc_host()
    world.clock.advance(minutes=56)
    assert await world.orch.gc_host()
    assert world.hetzner.servers == {}
