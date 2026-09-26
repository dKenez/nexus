"""The nexus Discord bot: slash commands over the orchestrator."""

import logging

import discord
from discord import app_commands

from nexus.bot import render
from nexus.bot.commands import GameGroup, HostGroup, games_command
from nexus.bot.permissions import MissingTier, RoleTiers
from nexus.core.notify import Event, Level
from nexus.core.orchestrator import NexusError, Orchestrator
from nexus.core.recipes import MissingSecretError, RecipeNotFoundError

log = logging.getLogger(__name__)


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
            await interaction.client.notify(
                Event(Level.ERROR, "nexus", f"/{name} failed", icon="🔴", details=(repr(original),))
            )
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

    def announces_in(self, channel_id: int | None) -> bool:
        """Whether orchestrator announcements should also go out for a command run in
        ``channel_id``: not when it's the notify channel, where the command's own progress
        message already says the same thing."""
        return self._notify_channel_id is not None and channel_id != self._notify_channel_id

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
        await self._check_notify_channel()

    async def _notify_channel(self) -> discord.abc.Messageable | None:
        assert self._notify_channel_id is not None
        channel = self.get_channel(self._notify_channel_id)
        if channel is None:
            channel = await self.fetch_channel(self._notify_channel_id)
        if not isinstance(channel, discord.abc.Messageable):
            log.error(
                "DISCORD_NOTIFY_CHANNEL_ID %s is a %s, which can't hold messages; "
                "use a text channel's id",
                self._notify_channel_id,
                getattr(channel, "type", type(channel).__name__),
            )
            return None
        return channel

    async def _check_notify_channel(self) -> None:
        """Fail loudly at startup, not silently at the first idle stop."""
        if self._notify_channel_id is None:
            log.warning("DISCORD_NOTIFY_CHANNEL_ID not set; idle stops and errors won't be posted")
            return
        try:
            channel = await self._notify_channel()
        except discord.Forbidden:
            log.error(
                "the bot can't see notify channel %s: in Discord, open the channel's "
                "Permissions and allow the bot View Channel, Send Messages and Embed Links",
                self._notify_channel_id,
            )
            return
        except discord.NotFound:
            log.error("notify channel %s doesn't exist (check the id)", self._notify_channel_id)
            return
        if channel is None:
            return
        me = getattr(channel, "guild", None) and channel.guild.me  # ty: ignore[unresolved-attribute]
        if me is not None and not channel.permissions_for(me).send_messages:  # ty: ignore[unresolved-attribute]
            log.error(
                "the bot can see notify channel #%s but can't send messages there; allow it "
                "Send Messages and Embed Links",
                getattr(channel, "name", self._notify_channel_id),
            )
            return
        log.info("notifications go to #%s", getattr(channel, "name", self._notify_channel_id))

    async def notify(self, event: Event) -> None:
        """``Notifier`` implementation: post the event to the notify channel as an embed."""
        if self._notify_channel_id is None or not self.is_ready():
            return
        channel = await self._notify_channel()
        if channel is not None:
            await channel.send(embed=render.event_embed(event))


async def respond(interaction: discord.Interaction, message: str) -> None:
    if interaction.response.is_done():
        await interaction.followup.send(message, ephemeral=True)
    else:
        await interaction.response.send_message(message, ephemeral=True)
