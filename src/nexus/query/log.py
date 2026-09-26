"""Readiness and player count from a game server's log (``query.type = "log"``)."""

from nexus.core.recipes import Query


def players_from_log(lines: list[str], query: Query) -> int | None:
    """Players connected per the log, or ``None`` while the server isn't ready yet.

    ``lines`` are the log lines containing any of the markers, oldest first. Each ``ready`` line
    starts a new server session: players from a previous run (before a crash-restart) are
    forgotten.
    """
    assert query.ready and query.join and query.leave
    ready = False
    connected: set[str] = set()
    for line in lines:
        if query.ready in line:
            ready = True
            connected.clear()
        elif (player := _after(line, query.join)) is not None:
            connected.add(player)
        elif (player := _after(line, query.leave)) is not None:
            connected.discard(player)
    return len(connected) if ready else None


def _after(line: str, marker: str) -> str | None:
    _, found, rest = line.partition(marker)
    if not found:
        return None
    tokens = rest.split()
    return tokens[0] if tokens else None
