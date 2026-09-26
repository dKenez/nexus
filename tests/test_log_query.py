"""Readiness and player counts from the Valheim server log (private servers don't answer A2S)."""

from nexus.core.recipes import Query
from nexus.db.models import GameStatus
from nexus.query.log import players_from_log
from tests.conftest import World

QUERY = Query.model_validate(
    {
        "type": "log",
        "ready": "Game server connected",
        "join": "Got handshake from client",
        "leave": "Closing socket",
    }
)
# Real lines from the dev server (lloesche/valheim-server, private), SteamIDs replaced.
BOOT = [
    "Sep 26 03:40:14 supervisord: valheim-server 09/26/2026 03:40:14: Registering lobby",
    "Sep 26 03:40:14 supervisord: valheim-server 09/26/2026 03:40:14: Game server connected",
]
PREFIX = "Sep 26 03:42:19 supervisord: valheim-server 09/26/2026 03:42:19: "
JOIN = PREFIX + "Got handshake from client 765611981"
LEAVE = PREFIX + "Closing socket 765611981"
JOIN2 = PREFIX + "Got handshake from client 765611982"


def test_not_ready_until_the_server_says_so() -> None:
    assert players_from_log([], QUERY) is None
    assert players_from_log(BOOT[:1], QUERY) is None
    assert players_from_log(BOOT, QUERY) == 0


def test_joins_and_leaves() -> None:
    assert players_from_log([*BOOT, JOIN], QUERY) == 1
    assert players_from_log([*BOOT, JOIN, JOIN2], QUERY) == 2
    assert players_from_log([*BOOT, JOIN, JOIN2, LEAVE], QUERY) == 1
    assert players_from_log([*BOOT, JOIN, JOIN], QUERY) == 1  # reconnect handshake


def test_wrong_password_attempt_counts_zero() -> None:
    # A refused join still logs a handshake, then the socket is closed.
    assert players_from_log([*BOOT, JOIN, LEAVE], QUERY) == 0


def test_crash_restart_forgets_old_players() -> None:
    # The container restarted after a crash: nobody from the first run is still connected.
    assert players_from_log([*BOOT, JOIN, JOIN2, *BOOT], QUERY) == 0


def use_log_query(world: World) -> None:
    recipes = world.orch.recipes._recipes
    recipes["alpha"] = recipes["alpha"].model_copy(update={"query": QUERY})


async def test_start_is_ready_when_the_log_says_so(world: World) -> None:
    use_log_query(world)
    world.agents.agent.logs["alpha"] = BOOT
    view = await world.orch.start("alpha")
    assert view.status is GameStatus.RUNNING
    assert view.last_error is None
    assert view.players == 0


async def test_private_server_with_players_is_not_idle_stopped(world: World) -> None:
    use_log_query(world)
    world.agents.agent.logs["alpha"] = BOOT
    await world.orch.start("alpha")
    world.agents.agent.logs["alpha"] = [*BOOT, JOIN]

    world.clock.advance(minutes=30)  # past grace and idle time
    assert await world.orch.poll_players() == []
    assert (await world.orch.game("alpha")).players == 1

    world.agents.agent.logs["alpha"] = [*BOOT, JOIN, LEAVE]
    world.clock.advance(minutes=19)
    assert await world.orch.poll_players() == []
    world.clock.advance(minutes=2)
    assert await world.orch.poll_players() == ["alpha"]
