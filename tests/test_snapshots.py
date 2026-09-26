import pytest
from pydantic import ValidationError

from nexus.core.notify import Level
from nexus.core.recipes import Snapshots
from nexus.infra.agent import parse_listing
from tests.conftest import World

T0 = 1_790_000_000.0


def enable_snapshots(world: World, **config: object) -> None:
    recipes = world.orch.recipes._recipes
    recipes["alpha"] = recipes["alpha"].model_copy(
        update={"snapshots": Snapshots(dir="backups", **config)}  # ty: ignore[invalid-argument-type]
    )


def write(world: World, name: str, content: bytes, age: float) -> None:
    world.agents.agent.files.setdefault("alpha", {})[f"backups/{name}"] = (
        content,
        world.agents.agent.remote_now - age,
    )


async def started(world: World) -> None:
    enable_snapshots(world, check_minutes=10, keep=2, settle_seconds=120)
    await world.orch.start("alpha")
    world.agents.agent.remote_now = T0


async def test_pulls_newest_settled_snapshot(world: World) -> None:
    await started(world)
    write(world, "worlds-1.zip", b"old", age=3600)
    write(world, "worlds-2.zip", b"new", age=600)
    write(world, "worlds-3.zip", b"being written", age=5)

    assert await world.orch.pull_snapshots() == ["worlds-2.zip"]

    snapshots = await world.orch.snapshots("alpha")
    assert [s.filename for s in snapshots] == ["worlds-2.zip"]
    stored = world.backups.snapshot_path("alpha", "worlds-2.zip")
    assert stored.read_bytes() == b"new"
    assert snapshots[0].taken_at.timestamp() == pytest.approx(T0 - 600)


async def test_checks_only_every_check_minutes_and_never_twice(world: World) -> None:
    await started(world)
    write(world, "worlds-1.zip", b"a", age=600)
    assert await world.orch.pull_snapshots() == ["worlds-1.zip"]

    write(world, "worlds-2.zip", b"b", age=300)
    world.clock.advance(minutes=5)
    assert await world.orch.pull_snapshots() == []  # not due yet

    world.clock.advance(minutes=6)
    assert await world.orch.pull_snapshots() == ["worlds-2.zip"]

    world.clock.advance(minutes=11)
    assert await world.orch.pull_snapshots() == []  # nothing new


async def test_retention_keeps_newest(world: World) -> None:
    await started(world)
    for i in range(1, 5):
        world.agents.agent.remote_now += 3600
        write(world, f"worlds-{i}.zip", str(i).encode(), age=600)
        world.clock.advance(minutes=11)
        await world.orch.pull_snapshots()

    snapshots = await world.orch.snapshots("alpha")
    assert [s.filename for s in snapshots] == ["worlds-4.zip", "worlds-3.zip"]
    on_disk = sorted(p.name for p in (world.backups.game_dir("alpha") / "snapshots").iterdir())
    assert on_disk == ["worlds-3.zip", "worlds-4.zip"]


async def test_failures_alert_once_and_leave_no_partial_file(world: World) -> None:
    await started(world)
    write(world, "worlds-1.zip", b"a", age=600)
    world.agents.agent.fail_read = True

    assert await world.orch.pull_snapshots() == []
    world.clock.advance(minutes=11)
    assert await world.orch.pull_snapshots() == []
    warnings = [m for level, m in world.notifier.messages if level is Level.WARNING]
    assert len(warnings) == 1
    assert list((world.backups.game_dir("alpha") / "snapshots").iterdir()) == []

    world.agents.agent.fail_read = False
    world.clock.advance(minutes=11)
    assert await world.orch.pull_snapshots() == ["worlds-1.zip"]


async def test_stopped_games_and_recipes_without_snapshots_are_skipped(world: World) -> None:
    await world.orch.start("beta")  # no snapshots configured
    await started(world)
    write(world, "worlds-1.zip", b"a", age=600)
    await world.orch.stop("alpha")
    assert await world.orch.pull_snapshots() == []


def test_snapshot_dir_must_stay_inside_data_path() -> None:
    for bad in ["/etc", "../x", "a/../../b", ""]:
        with pytest.raises(ValidationError):
            Snapshots(dir=bad)
    assert Snapshots(dir="backups/").dir == "backups"


def test_parse_listing() -> None:
    listing = parse_listing("1790000000.5\nworlds-1.zip\t123\t1789999000.25\nodd name.zip\t1\t2\n")
    assert listing.now == 1790000000.5
    assert [(f.name, f.size, f.mtime) for f in listing.files] == [
        ("worlds-1.zip", 123, 1789999000.25),
        ("odd name.zip", 1, 2.0),
    ]
