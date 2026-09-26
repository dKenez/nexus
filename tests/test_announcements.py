"""Every start, stop and VM deletion is announced, in the /games style, whoever triggered it."""

import dataclasses
from datetime import timedelta
from zoneinfo import ZoneInfo

from nexus.bot import render
from nexus.bot.client import NexusBot
from nexus.bot.permissions import RoleTiers
from nexus.core.notify import Event, Level, Tone
from nexus.core.timefmt import duration
from tests.conftest import World


def texts(world: World) -> list[str]:
    return [message for _, message in world.notifier.messages]


def bill_like_hetzner(world: World) -> None:
    world.orch.config = dataclasses.replace(world.orch.config, host_billing_margin=300)


def in_copenhagen(world: World) -> None:
    world.orch.config = dataclasses.replace(
        world.orch.config, timezone=ZoneInfo("Europe/Copenhagen")
    )


async def test_start_and_stop_are_announced(world: World) -> None:
    await world.orch.start("alpha")
    up = world.notifier.events[0]
    assert (up.title, up.icon, up.summary, up.colour) == ("Alpha", "🟢", "up", Tone.GOOD)
    assert up.details == ("`203.0.113.10:2456`", "started in 0s (new VM)")

    world.clock.advance(minutes=10)
    await world.orch.stop("alpha")
    assert texts(world)[1:] == [
        "Alpha: stopped and backed up",
        "Host: VM deleted · up 10m · 1 hour billed · about €0.01 (nexus-test-20260926-120000)",
    ]


async def test_start_time_is_measured_from_the_request(world: World) -> None:
    op = world.orch.begin("alpha", "starting")  # the request arrives...
    world.clock.advance(minutes=2, seconds=13)  # ...the VM takes a while...
    view = await world.orch.start("alpha", op=op)
    assert view.start_seconds == 133
    assert view.start_new_vm is True
    assert world.notifier.events[-1].details[-1] == "started in 2m 13s (new VM)"


async def test_start_on_a_kept_vm_says_so(world: World) -> None:
    bill_like_hetzner(world)
    await world.orch.start("alpha")
    await world.orch.stop("alpha")
    await world.orch.start("alpha")
    assert world.notifier.events[-1].details[-1] == "started in 0s (VM reused)"
    assert (await world.orch.game("alpha")).start_new_vm is False


async def test_kept_vm_is_in_the_stop_message_and_deleted_later(world: World) -> None:
    bill_like_hetzner(world)
    in_copenhagen(world)
    await world.orch.start("alpha")
    world.clock.advance(minutes=10)
    await world.orch.stop("alpha")
    stop = world.notifier.events[-1]
    assert stop.summary == "stopped and backed up"
    # 12:55 UTC is 14:55 in Copenhagen (CEST); no "(already paid for)".
    assert stop.footer == (
        "The VM stays up until 14:55 CEST; starting a game before then reuses it."
    )
    world.clock.advance(minutes=46)
    assert await world.orch.gc_host()
    assert texts(world)[-1] == (
        "Host: VM deleted · up 56m · 1 hour billed · about €0.01 (nexus-test-20260926-120000)"
    )


async def test_long_session_cost(world: World) -> None:
    await world.orch.start("alpha")
    world.clock.advance(hours=2, minutes=10)
    await world.orch.stop("alpha")
    assert world.notifier.events[-1].details == ("up 2h 10m", "3 hours billed", "about €0.04")


async def test_idle_stop_is_announced_once(world: World) -> None:
    await world.orch.start("alpha")
    world.clock.advance(minutes=21)
    assert await world.orch.poll_players() == ["alpha"]
    stops = [e for e in world.notifier.events if e.summary == "stopped and backed up"]
    assert [e.details for e in stops] == [("empty for 21 minutes",)]


async def test_shutdown_does_not_promise_the_vm_stays(world: World) -> None:
    bill_like_hetzner(world)
    await world.orch.start("alpha")
    await world.orch.shutdown_host()
    assert not any("stays up" in t for t in texts(world))
    assert world.notifier.events[-1].summary == "VM deleted"


async def test_announce_false_is_silent_except_for_the_vm(world: World) -> None:
    await world.orch.start("alpha", announce=False)
    await world.orch.stop("alpha", announce=False)
    assert all(e.title == "Host" for e in world.notifier.events)


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


def test_event_embed_matches_the_games_style() -> None:
    event = Event(
        Level.INFO,
        "Valheim",
        "up",
        icon="🟢",
        details=("`1.2.3.4:2456`", "started in 2m 13s (new VM)"),
        tone=Tone.GOOD,
    )
    embed = render.event_embed(event)
    assert embed.title == "Valheim"
    assert embed.description == "🟢 **up** · `1.2.3.4:2456` · started in 2m 13s (new VM)"
    assert embed.colour == render.COLOURS[Tone.GOOD]
    error = render.event_embed(Event(Level.ERROR, "Valheim", "stop failed", icon="🔴"))
    assert error.colour == render.COLOURS[Tone.ERROR]


def test_durations() -> None:
    assert duration(45) == "45s"
    assert duration(133) == "2m 13s"
    assert duration(timedelta(hours=1, minutes=5)) == "1h 05m"
