"""The nexus Discord bot: slash commands over the orchestrator."""

import logging

import discord
from discord import app_commands

from nexus.bot.commands import GameGroup, HostGroup, games_command
from nexus.bot.permissions import MissingTier, RoleTiers
from nexus.core.notify import Level
from nexus.core.orchestrator import NexusError, Orchestrator
from nexus.core.recipes import MissingSecretError, RecipeNotFoundError

log = logging.getLogger(__name__)

LEVEL_ICON = {Level.INFO: "\N{INFORMATION SOURCE}\ufe0f", Level.WARNING: "⚠️", Level.ERROR: "🚨"}


class NexusTree(app_commands.CommandTree["NexusBot"]):
    # Same signature as the base; ty doesn't resolve the ClientT specialisation here.
    async def on_error(  # ty: ignore[invalid-method-override]
        self, interaction: discord.Interaction["NexusBot"], error: app_commands.AppCommandError, /
    ) -> None:
        original = getattr(error, "original", error)
        if isinstance(error, MissingTier):
            message = f"🔒 Sorry, {error}."
        elif isinstance(original, (NexusError, RecipeNotFoundError)):
            message = f"🔴 {original}"
        elif isinstance(original, MissingSecretError):
            message = f"🔴 Misconfigured: {original}"
            log.error("%s", original)
        else:
            log.exception("command failed", exc_info=original)
            message = "🔴 Something went wrong; the admins have been told."
            name = interaction.command.qualified_name if interaction.command else "?"
            await interaction.client.notify(Level.ERROR, f"`/{name}` failed: {original!r}")
        await respond(interaction, message)


class NexusBot(discord.Client):
    def __init__(
        self,
        *,
        orchestrator: Orchestrator,
        role_tiers: RoleTiers,
        guild_id: int | None,
        notify_channel_id: int | None,
    ) -> None:
        intents = discord.Intents.none()
        intents.guilds = True
        super().__init__(intents=intents)
        self.orchestrator = orchestrator
        self.role_tiers = role_tiers
        self._guild_id = guild_id
        self._notify_channel_id = notify_channel_id
        self.tree = NexusTree(self)
        self.tree.add_command(games_command)
        self.tree.add_command(GameGroup())
        self.tree.add_command(HostGroup())

    async def setup_hook(self) -> None:
        if self._guild_id is not None:
            guild = discord.Object(id=self._guild_id)
            self.tree.copy_global_to(guild=guild)
            synced = await self.tree.sync(guild=guild)
        else:
            synced = await self.tree.sync()
        log.info("synced %d commands", len(synced))

    async def on_ready(self) -> None:
        log.info("discord bot ready as %s", self.user)

    async def notify(self, level: Level, message: str) -> None:
        """``Notifier`` implementation: post to the notify channel."""
        if self._notify_channel_id is None or not self.is_ready():
            return
        channel = self.get_channel(self._notify_channel_id)
        if channel is None:
            channel = await self.fetch_channel(self._notify_channel_id)
        if isinstance(channel, discord.abc.Messageable):
            await channel.send(f"{LEVEL_ICON[level]} {message}"[:2000])


async def respond(interaction: discord.Interaction, message: str) -> None:
    if interaction.response.is_done():
        await interaction.followup.send(message, ephemeral=True)
    else:
        await interaction.response.send_message(message, ephemeral=True)
