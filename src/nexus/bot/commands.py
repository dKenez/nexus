"""Slash commands. Viewers look, operators start/stop, admins manage backups and the host."""

import logging
from typing import TYPE_CHECKING

import discord
from discord import app_commands

from nexus.bot import render
from nexus.bot.permissions import Tier, require
from nexus.core.notify import Event, Level, Tone
from nexus.core.orchestrator import Orchestrator
from nexus.core.timefmt import date_clock
from nexus.db.models import GameStatus, utcnow

if TYPE_CHECKING:
    from nexus.bot.client import NexusBot

log = logging.getLogger(__name__)


def orch(interaction: discord.Interaction) -> Orchestrator:
    client: NexusBot = interaction.client  # ty: ignore[invalid-assignment]
    return client.orchestrator


def announce(interaction: discord.Interaction) -> bool:
    client: NexusBot = interaction.client  # ty: ignore[invalid-assignment]
    return client.announces_in(interaction.channel_id)


def actor(interaction: discord.Interaction) -> str:
    return f"discord:{interaction.user.id}"


class Progress:
    """A status embed edited in place as an operation advances, in the /games style: the
    current status on top, the steps so far as small text underneath.

    Interaction webhooks expire after 15 minutes and a first boot can take longer, so if an
    edit fails the status continues in a fresh channel message.
    """

    def __init__(self, interaction: discord.Interaction, title: str, working: str) -> None:
        self._interaction = interaction
        self._title = title
        self._head = f"⏳ **{working}**"
        self._message: discord.Message | discord.WebhookMessage | None = None
        self._steps: list[str] = []
        self._requested = f"requested by {interaction.user.display_name}"

    def _embed(self, head: str, colour: Tone, footer: str | None = None) -> discord.Embed:
        steps = "\n".join(f"-# {step}" for step in self._steps[-8:])
        embed = discord.Embed(
            title=self._title,
            description=f"{head}\n{steps}" if steps else head,
            colour=render.COLOURS[colour],
        )
        embed.set_footer(text="\n".join(t for t in (footer, self._requested) if t))
        return embed

    async def begin(self) -> None:
        await self._interaction.response.send_message(embed=self._embed(self._head, Tone.NEUTRAL))
        self._message = await self._interaction.original_response()

    async def __call__(self, step: str) -> None:
        self._steps.append(step)
        await self._edit(self._embed(self._head, Tone.NEUTRAL))

    async def finish(self, event: Event) -> None:
        await self._edit(self._embed(event.line(), event.colour, event.footer))

    async def _edit(self, embed: discord.Embed) -> None:
        if self._message is None:
            return
        try:
            await self._message.edit(content=None, embed=embed)
        except discord.HTTPException:
            channel = self._interaction.channel
            if isinstance(channel, discord.abc.Messageable):
                self._message = await channel.send(embed=embed)


def failed(title: str, what: str, exc: BaseException) -> Event:
    return Event(Level.ERROR, title, what, icon="🔴", details=(str(exc),))


async def game_autocomplete(
    interaction: discord.Interaction, current: str
) -> list[app_commands.Choice[str]]:
    return [
        app_commands.Choice(name=r.display_name, value=r.name)
        for r in orch(interaction).recipes.enabled()
        if current.lower() in r.name or current.lower() in r.display_name.lower()
    ][:25]


async def restore_point_autocomplete(
    interaction: discord.Interaction, current: str
) -> list[app_commands.Choice[str]]:
    """Full backups and snapshots, newest first; values are "backup:<id>" / "snapshot:<id>"."""
    game = getattr(interaction.namespace, "game", None)
    if not game or game not in orch(interaction).recipes:
        return []
    o = orch(interaction)
    tz = o.config.timezone
    points = [
        (
            b.created_at,
            f"Backup {date_clock(b.created_at, tz)} · {b.reason} · {render.size(b.bytes)}",
            f"backup:{b.id}",
        )
        for b in await o.backups(game)
    ] + [
        (
            sn.taken_at,
            f"Snapshot {date_clock(sn.taken_at, tz)} · {render.size(sn.bytes)}",
            f"snapshot:{sn.id}",
        )
        for sn in await o.snapshots(game)
    ]
    points.sort(key=lambda p: p[0], reverse=True)
    return [
        app_commands.Choice(name=name, value=value)
        for _, name, value in points
        if current.lower() in name.lower()
    ][:25]


@app_commands.command(name="games", description="List the game servers and who's playing")
@require(Tier.VIEWER)
async def games_command(interaction: discord.Interaction) -> None:
    o = orch(interaction)
    embed = render.games_embed(await o.games(), await o.host(), utcnow(), await o.address())
    await interaction.response.send_message(embed=embed)


class GameGroup(app_commands.Group):
    def __init__(self) -> None:
        super().__init__(name="game", description="Manage a game server")

    @app_commands.command(description="Show one game server")
    @app_commands.describe(game="The game")
    @app_commands.autocomplete(game=game_autocomplete)
    @require(Tier.VIEWER)
    async def status(self, interaction: discord.Interaction, game: str) -> None:
        o = orch(interaction)
        view = await o.game(game)
        await interaction.response.send_message(
            embed=render.game_embed(view, utcnow(), await o.address())
        )

    @app_commands.command(description="Start a game server (provisions a VM if needed)")
    @app_commands.describe(game="The game to start")
    @app_commands.autocomplete(game=game_autocomplete)
    @require(Tier.OPERATOR)
    async def start(self, interaction: discord.Interaction, game: str) -> None:
        o = orch(interaction)
        recipe = o.recipes.get(game)
        op = o.begin(game, "starting")  # duplicate clicks get "already starting"
        progress = Progress(interaction, recipe.display_name, "starting")
        try:
            await progress.begin()
        except BaseException:
            op.release()
            raise
        try:
            view = await o.start(game, progress, op=op, announce=announce(interaction))
        except Exception as exc:
            await o.audit(actor(interaction), "start", game, "error", str(exc))
            await progress.finish(failed(recipe.display_name, "start failed", exc))
            return
        await o.audit(actor(interaction), "start", game, "ok")
        if view.status is GameStatus.RUNNING and view.last_error is None:
            await progress.finish(await o.up_event(recipe, view.start_seconds, view.start_new_vm))
        else:
            await progress.finish(
                Event(
                    Level.WARNING,
                    recipe.display_name,
                    "started, but not answering yet",
                    icon="🟡",
                    details=(await o.endpoint(recipe),),
                )
            )

    @app_commands.command(description="Stop a game server and back it up")
    @app_commands.describe(game="The game to stop")
    @app_commands.autocomplete(game=game_autocomplete)
    @require(Tier.OPERATOR)
    async def stop(self, interaction: discord.Interaction, game: str) -> None:
        o = orch(interaction)
        recipe = o.recipes.get(game)
        op = o.begin(game, "stopping")  # duplicate clicks get "already stopping"
        progress = Progress(interaction, recipe.display_name, "stopping")
        try:
            await progress.begin()
        except BaseException:
            op.release()
            raise
        try:
            await o.stop(game, "manual", progress, op=op, announce=announce(interaction))
        except Exception as exc:
            await o.audit(actor(interaction), "stop", game, "error", str(exc))
            await progress.finish(failed(recipe.display_name, "stop failed", exc))
            return
        await o.audit(actor(interaction), "stop", game, "ok")
        host = await o.host()
        await progress.finish(
            Event(
                Level.INFO,
                recipe.display_name,
                "stopped and backed up",
                icon="⚫",
                footer=o.vm_kept_note(host) if host else None,
            )
        )

    @app_commands.command(description="Back up a running game now (restarts it briefly)")
    @app_commands.autocomplete(game=game_autocomplete)
    @require(Tier.ADMIN)
    async def backup(self, interaction: discord.Interaction, game: str) -> None:
        o = orch(interaction)
        recipe = o.recipes.get(game)
        op = o.begin(game, "being backed up")  # duplicate clicks get "already being backed up"
        progress = Progress(interaction, recipe.display_name, "backing up")
        try:
            await progress.begin()
        except BaseException:
            op.release()
            raise
        try:
            backup = await o.backup(game, progress, op=op)
        except Exception as exc:
            await o.audit(actor(interaction), "backup", game, "error", str(exc))
            await progress.finish(failed(recipe.display_name, "backup failed", exc))
            return
        await o.audit(actor(interaction), "backup", game, "ok", backup.filename)
        await progress.finish(
            Event(
                Level.INFO,
                recipe.display_name,
                "backed up",
                icon="💾",
                details=(f"`#{backup.id}`", render.size(backup.bytes)),
                tone=Tone.GOOD,
            )
        )

    @app_commands.command(description="List a game's backups")
    @app_commands.autocomplete(game=game_autocomplete)
    @require(Tier.ADMIN)
    async def backups(self, interaction: discord.Interaction, game: str) -> None:
        o = orch(interaction)
        embed = render.backups_embed(
            o.recipes.get(game).display_name,
            await o.backups(game),
            await o.snapshots(game),
            o.config.timezone,
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @app_commands.command(description="Choose the backup or snapshot a game's next start uses")
    @app_commands.describe(point="A full backup or an hourly snapshot")
    @app_commands.autocomplete(game=game_autocomplete)
    @app_commands.autocomplete(point=restore_point_autocomplete)
    @require(Tier.ADMIN)
    async def restore(self, interaction: discord.Interaction, game: str, point: str) -> None:
        o = orch(interaction)
        kind, _, raw_id = point.partition(":")
        if kind not in ("backup", "snapshot") or not raw_id.isdigit():
            await interaction.response.send_message(
                "Pick a backup or snapshot from the list.", ephemeral=True
            )
            return
        await interaction.response.defer(thinking=True)
        if kind == "snapshot":
            chosen = await o.restore_snapshot(game, int(raw_id))
            what = "the chosen snapshot"
        else:
            chosen = await o.pin_backup(game, int(raw_id))
            when = date_clock(chosen.created_at, o.config.timezone)
            what = f"backup `#{chosen.id}` ({when})"
        await o.audit(actor(interaction), "restore", game, "ok", chosen.filename)
        await interaction.followup.send(f"📌 {game} will start from {what}.")


class HostGroup(app_commands.Group):
    def __init__(self) -> None:
        super().__init__(name="host", description="The Hetzner VM that runs the games")

    @app_commands.command(description="Show the VM, its cost and what runs on it")
    @require(Tier.VIEWER)
    async def status(self, interaction: discord.Interaction) -> None:
        o = orch(interaction)
        host = await o.host()
        running = [v for v in await o.games() if v.status is not GameStatus.STOPPED]
        price = await o.hourly_price() if host else None
        await interaction.response.send_message(
            embed=render.host_embed(host, running, price, utcnow(), o.config.timezone)
        )

    @app_commands.command(description="Stop and back up every game, then delete the VM")
    @require(Tier.ADMIN)
    async def shutdown(self, interaction: discord.Interaction) -> None:
        o = orch(interaction)
        op = o.begin_host("shutting down")
        progress = Progress(interaction, "Host", "shutting down")
        try:
            await progress.begin()
        except BaseException:
            op.release()
            raise
        try:
            await o.shutdown_host(progress, op=op, announce=announce(interaction))
        except Exception as exc:
            await o.audit(actor(interaction), "host-shutdown", None, "error", str(exc))
            await progress.finish(failed("Host", "shutdown failed", exc))
            return
        await o.audit(actor(interaction), "host-shutdown", None, "ok")
        await progress.finish(
            Event(
                Level.INFO,
                "Host",
                "shut down",
                icon="⚫",
                details=("all games stopped and backed up", "VM deleted"),
            )
        )

    @app_commands.command(description="Delete the VM immediately, even with unsaved data")
    @app_commands.describe(confirm="Type DESTROY to confirm")
    @require(Tier.ADMIN)
    async def destroy(self, interaction: discord.Interaction, confirm: str) -> None:
        if confirm != "DESTROY":
            await interaction.response.send_message(
                "Not confirmed; nothing was deleted.", ephemeral=True
            )
            return
        await interaction.response.defer(thinking=True)
        o = orch(interaction)
        deleted = await o.destroy_host()
        await o.audit(actor(interaction), "host-destroy", None, "ok")
        await interaction.followup.send("💥 VM deleted." if deleted else "There was no VM.")
