"""Discord role tiers: viewer ⊂ operator ⊂ admin."""

from collections.abc import Iterable
from dataclasses import dataclass
from enum import IntEnum

import discord
from discord import app_commands


class Tier(IntEnum):
    VIEWER = 1
    OPERATOR = 2
    ADMIN = 3


@dataclass(frozen=True)
class RoleTiers:
    viewer: frozenset[int]
    operator: frozenset[int]
    admin: frozenset[int]

    @classmethod
    def of(
        cls, viewer: Iterable[int], operator: Iterable[int], admin: Iterable[int]
    ) -> "RoleTiers":
        return cls(frozenset(viewer), frozenset(operator), frozenset(admin))

    def tier_of(self, role_ids: Iterable[int]) -> Tier | None:
        roles = set(role_ids)
        if roles & self.admin:
            return Tier.ADMIN
        if roles & self.operator:
            return Tier.OPERATOR
        # No viewer roles configured means every guild member may look.
        if not self.viewer or roles & self.viewer:
            return Tier.VIEWER
        return None


class MissingTier(app_commands.CheckFailure):
    def __init__(self, tier: Tier) -> None:
        super().__init__(f"this needs the {tier.name.lower()} role")
        self.tier = tier


def require(tier: Tier):
    """``app_commands.check`` that the invoking member has at least ``tier``."""

    async def predicate(interaction: discord.Interaction) -> bool:
        member = interaction.user
        tiers: RoleTiers = interaction.client.role_tiers  # ty: ignore[unresolved-attribute]
        role_ids = [r.id for r in getattr(member, "roles", [])]
        have = tiers.tier_of(role_ids)
        if have is None or have < tier:
            raise MissingTier(tier)
        return True

    return app_commands.check(predicate)
