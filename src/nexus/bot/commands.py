"""Slash commands. Viewers look, operators start/stop, admins manage backups and the host."""

import logging
from typing import TYPE_CHECKING

import discord
from discord import app_commands

from nexus.bot import render
from nexus.bot.permissions import Tier, require
from nexus.core.orchestrator import Orchestrator
from nexus.db.models import GameStatus, utcnow

if TYPE_CHECKING:
    from nexus.bot.client import NexusBot

log = logging.getLogger(__name__)


def orch(interaction: discord.Interaction) -> Orchestrator:
    client: NexusBot = interaction.client  # ty: ignore[invalid-assignment]
    return client.orchestrator


def actor(interaction: discord.Interaction) -> str:
    return f"discord:{interaction.user.id}"


class Progress:
    """A status message edited in place as an operation advances.

    Interaction webhooks expire after 15 minutes and a first boot can take longer, so the
    message is a plain channel message whenever the channel allows it.
    """

    def __init__(self, interaction: discord.Interaction, title: str) -> None:
        self._interaction = interaction
        self._title = title
        self._message: discord.Message | discord.WebhookMessage | None = None
        self._lines: list[str] = []

    def _render(self, final: str | None = None) -> str:
        body = "\n".join(f"· {line}" for line in self._lines[-8:])
        text = f"{self._title}\n{body}" if body else self._title
        return f"{text}\n{final}" if final else text

    async def begin(self) -> None:
        await self._interaction.response.send_message(f"⏳ {self._title}")
        self._message = await self._interaction.original_response()

    async def __call__(self, line: str) -> None:
        self._lines.append(line)
        await self._edit(f"⏳ {self._render()}")

    async def finish(self, final: str) -> None:
        await self._edit(self._render(final))

    async def _edit(self, content: str) -> None:
        if self._message is None:
            return
        try:
            await self._message.edit(content=content[:2000])
        except discord.HTTPException:
            # Token expired: continue in a fresh channel message.
            channel = self._interaction.channel
            if isinstance(channel, discord.abc.Messageable):
                self._message = await channel.send(content[:2000])


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
    points = [
        (
            b.created_at,
            f"Backup {b.created_at:%Y-%m-%d %H:%M} UTC · {b.reason} · {render.size(b.bytes)}",
            f"backup:{b.id}",
        )
        for b in await o.backups(game)
    ] + [
        (
            sn.taken_at,
            f"Snapshot {sn.taken_at:%Y-%m-%d %H:%M} UTC · {render.size(sn.bytes)}",
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
            f"**{view.recipe.display_name}**: {render.game_line(view, utcnow(), await o.address())}"
        )

    @app_commands.command(description="Start a game server (provisions a VM if needed)")
    @app_commands.describe(game="The game to start")
    @app_commands.autocomplete(game=game_autocomplete)
    @require(Tier.OPERATOR)
    async def start(self, interaction: discord.Interaction, game: str) -> None:
        o = orch(interaction)
        recipe = o.recipes.get(game)
        op = o.begin(game, "starting")  # duplicate clicks get "already starting"
        progress = Progress(
            interaction, f"Starting **{recipe.display_name}** for {interaction.user.mention}"
        )
        try:
            await progress.begin()
        except BaseException:
            op.release()
            raise
        try:
            view = await o.start(game, progress, op=op)
        except Exception as exc:
            await o.audit(actor(interaction), "start", game, "error", str(exc))
            await progress.finish(f"🔴 {exc}")
            return
        await o.audit(actor(interaction), "start", game, "ok")
        address = await o.address()
        ports = ", ".join(str(p) for p in recipe.ports)
        endpoint = f"{address}:{recipe.ports[0].port}"
        if view.status is GameStatus.RUNNING and view.last_error is None:
            await progress.finish(f"🟢 **{recipe.display_name}** is up at `{endpoint}` ({ports})")
        else:
            await progress.finish(
                f"🟡 **{recipe.display_name}** started but isn't answering yet: {view.last_error}"
            )

    @app_commands.command(description="Stop a game server and back it up")
    @app_commands.describe(game="The game to stop")
    @app_commands.autocomplete(game=game_autocomplete)
    @require(Tier.OPERATOR)
    async def stop(self, interaction: discord.Interaction, game: str) -> None:
        o = orch(interaction)
        recipe = o.recipes.get(game)
        op = o.begin(game, "stopping")  # duplicate clicks get "already stopping"
        progress = Progress(
            interaction, f"Stopping **{recipe.display_name}** for {interaction.user.mention}"
        )
        try:
            await progress.begin()
        except BaseException:
            op.release()
            raise
        try:
            await o.stop(game, "manual", progress, op=op)
        except Exception as exc:
            await o.audit(actor(interaction), "stop", game, "error", str(exc))
            await progress.finish(f"🔴 {exc}")
            return
        await o.audit(actor(interaction), "stop", game, "ok")
        host = await o.host()
        tail = "" if host else " The VM was deleted."
        await progress.finish(f"⚫ **{recipe.display_name}** stopped and backed up.{tail}")

    @app_commands.command(description="Back up a running game now (restarts it briefly)")
    @app_commands.autocomplete(game=game_autocomplete)
    @require(Tier.ADMIN)
    async def backup(self, interaction: discord.Interaction, game: str) -> None:
        o = orch(interaction)
        recipe = o.recipes.get(game)
        op = o.begin(game, "being backed up")  # duplicate clicks get "already being backed up"
        progress = Progress(interaction, f"Backing up **{recipe.display_name}**")
        try:
            await progress.begin()
        except BaseException:
            op.release()
            raise
        try:
            backup = await o.backup(game, progress, op=op)
        except Exception as exc:
            await o.audit(actor(interaction), "backup", game, "error", str(exc))
            await progress.finish(f"🔴 {exc}")
            return
        await o.audit(actor(interaction), "backup", game, "ok", backup.filename)
        await progress.finish(f"💾 Saved backup `#{backup.id}` ({render.size(backup.bytes)}).")

    @app_commands.command(description="List a game's backups")
    @app_commands.autocomplete(game=game_autocomplete)
    @require(Tier.ADMIN)
    async def backups(self, interaction: discord.Interaction, game: str) -> None:
        o = orch(interaction)
        text = render.backups_text(game, await o.backups(game), await o.snapshots(game))
        await interaction.response.send_message(text, ephemeral=True)

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
            what = f"backup `#{chosen.id}` ({chosen.created_at:%Y-%m-%d %H:%M} UTC)"
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
            embed=render.host_embed(host, running, price, utcnow())
        )

    @app_commands.command(description="Stop and back up every game, then delete the VM")
    @require(Tier.ADMIN)
    async def shutdown(self, interaction: discord.Interaction) -> None:
        o = orch(interaction)
        op = o.begin_host("shutting down")
        progress = Progress(interaction, "Shutting down the host")
        try:
            await progress.begin()
        except BaseException:
            op.release()
            raise
        try:
            await o.shutdown_host(progress, op=op)
        except Exception as exc:
            await o.audit(actor(interaction), "host-shutdown", None, "error", str(exc))
            await progress.finish(f"🔴 {exc}")
            return
        await o.audit(actor(interaction), "host-shutdown", None, "ok")
        await progress.finish("⚫ All games stopped and backed up; the VM is gone.")

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
