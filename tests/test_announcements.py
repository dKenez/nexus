"""Every start, stop and VM deletion is announced, whoever triggered it."""

import dataclasses

from nexus.bot.client import NexusBot
from nexus.bot.permissions import RoleTiers
from tests.conftest import World


def texts(world: World) -> list[str]:
    return [message for _, message in world.notifier.messages]


def bill_like_hetzner(world: World) -> None:
    world.orch.config = dataclasses.replace(world.orch.config, host_billing_margin=300)


async def test_start_and_stop_are_announced(world: World) -> None:
    await world.orch.start("alpha")
    assert texts(world) == ["Alpha is up at `203.0.113.10:2456`."]

    world.clock.advance(minutes=10)
    await world.orch.stop("alpha")
    assert texts(world)[1:] == [
        "Alpha stopped and backed up.",
        "VM nexus-test-20260926-120000 deleted: up 10m, 1 hour billed, about €0.01.",
    ]


async def test_kept_vm_is_announced_and_its_deletion_later(world: World) -> None:
    bill_like_hetzner(world)
    await world.orch.start("alpha")
    world.clock.advance(minutes=10)
    await world.orch.stop("alpha")
    assert texts(world)[1:] == [
        "Alpha stopped and backed up.",
        "The VM stays up until 12:55 UTC (already paid for); "
        "starting a game before then reuses it.",
    ]
    world.clock.advance(minutes=46)
    assert await world.orch.gc_host()
    assert texts(world)[-1] == (
        "VM nexus-test-20260926-120000 deleted: up 56m, 1 hour billed, about €0.01."
    )


async def test_long_session_cost(world: World) -> None:
    await world.orch.start("alpha")
    world.clock.advance(hours=2, minutes=10)
    await world.orch.stop("alpha")
    assert texts(world)[-1].endswith("up 2h 10m, 3 hours billed, about €0.04.")


async def test_idle_stop_is_announced_once(world: World) -> None:
    await world.orch.start("alpha")
    world.clock.advance(minutes=21)
    assert await world.orch.poll_players() == ["alpha"]
    stop_messages = [t for t in texts(world) if "backed up" in t]
    assert stop_messages == ["Alpha was empty for 21 minutes; stopped and backed up."]


async def test_shutdown_does_not_promise_the_vm_stays(world: World) -> None:
    bill_like_hetzner(world)
    await world.orch.start("alpha")
    await world.orch.shutdown_host()
    assert not any("stays up" in t for t in texts(world))
    assert "deleted" in texts(world)[-1]


async def test_announce_false_is_silent_except_for_the_vm(world: World) -> None:
    await world.orch.start("alpha", announce=False)
    await world.orch.stop("alpha", announce=False)
    assert all(t.startswith("VM ") for t in texts(world))


def test_bot_skips_duplicates_in_the_notify_channel(world: World) -> None:
    bot = NexusBot(
        orchestrator=world.orch,
        role_tiers=RoleTiers.of([], [], []),
        guild_id=None,
        notify_channel_id=42,
    )
    assert not bot.announces_in(42)  # the progress message there already says it
    assert bot.announces_in(7)
    assert bot.announces_in(None)
