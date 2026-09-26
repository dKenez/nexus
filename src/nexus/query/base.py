"""Player-count queries. One function per protocol, selected by the recipe's ``query.type``."""

from typing import Protocol

from nexus.core.recipes import QueryType, Recipe
from nexus.query import a2s


class PlayerQuery(Protocol):
    async def __call__(self, recipe: Recipe, host: str) -> int | None:
        """The current player count, or ``None`` if the server doesn't answer."""
        ...


async def query_players(recipe: Recipe, host: str, timeout: float = 3.0) -> int | None:
    """Network queries run from nexus. (``log`` queries go through the host agent instead.)"""
    match recipe.query.type:
        case QueryType.A2S:
            assert recipe.query.port is not None
            return await a2s.player_count(host, recipe.query.port, timeout)
        case QueryType.LOG | QueryType.NONE:
            return None
