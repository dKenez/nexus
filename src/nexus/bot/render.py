"""Rendering orchestrator state for Discord."""

from datetime import datetime

import discord

from nexus.core.orchestrator import GameView, HostView
from nexus.db.models import Backup, GameStatus, HostStatus

STATUS_ICON = {
    GameStatus.STOPPED: "⚫",
    GameStatus.STARTING: "🟡",
    GameStatus.RESTORING: "🟡",
    GameStatus.RUNNING: "🟢",
    GameStatus.STOPPING: "🟠",
    GameStatus.BACKING_UP: "🟠",
    GameStatus.FAILED: "🔴",
}


def duration(seconds: float) -> str:
    seconds = int(seconds)
    hours, rem = divmod(seconds, 3600)
    minutes = rem // 60
    return f"{hours}h{minutes:02d}m" if hours else f"{minutes}m"


def size(num: int) -> str:
    value = float(num)
    for unit in ("B", "KiB", "MiB", "GiB"):
        if value < 1024:
            return f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} TiB"


def game_line(view: GameView, now: datetime, address: str) -> str:
    parts = [f"{STATUS_ICON[view.status]} **{view.status.replace('_', ' ')}**"]
    if view.status is GameStatus.RUNNING:
        if view.players is not None:
            parts.append(f"{view.players} player{'s' if view.players != 1 else ''}")
        if view.started_at:
            parts.append(f"up {duration((now - view.started_at).total_seconds())}")
        port = view.recipe.ports[0].port
        parts.append(f"`{address}:{port}`")
    line = " · ".join(parts)
    if view.last_error and view.status in (GameStatus.FAILED, GameStatus.STOPPED):
        line += f"\n⚠️ {view.last_error[:200]}"
    return line


def games_embed(
    views: list[GameView], host: HostView | None, now: datetime, address: str
) -> discord.Embed:
    embed = discord.Embed(title="Game servers", colour=discord.Colour.dark_teal())
    for view in views:
        if not view.recipe.enabled:
            continue
        embed.add_field(
            name=view.recipe.display_name, value=game_line(view, now, address), inline=False
        )
    if host is None:
        embed.set_footer(text="No VM running — nothing is being billed.")
    else:
        embed.set_footer(text=f"VM {host.name} · {host.server_type} · {host.status}")
    return embed


def host_embed(
    host: HostView | None, running: list[GameView], price: str | None, now: datetime
) -> discord.Embed:
    if host is None:
        return discord.Embed(
            title="Host", description="No VM exists right now.", colour=discord.Colour.greyple()
        )
    colour = discord.Colour.green() if host.status is HostStatus.READY else discord.Colour.orange()
    embed = discord.Embed(title=f"Host {host.name}", colour=colour)
    embed.add_field(name="Status", value=str(host.status))
    embed.add_field(name="Type", value=f"{host.server_type} ({host.memory_mb // 1024} GB)")
    embed.add_field(name="IP", value=host.ip)
    embed.add_field(name="Up", value=duration((now - host.created_at).total_seconds()))
    if price:
        embed.add_field(name="Price", value=f"€{float(price):.4f}/h")
    embed.add_field(
        name="Games",
        value=", ".join(v.recipe.display_name for v in running) or "none",
        inline=False,
    )
    if host.last_error:
        embed.add_field(name="Last error", value=host.last_error[:500], inline=False)
    return embed


def backups_text(game: str, backups: list[Backup]) -> str:
    if not backups:
        return f"No backups for {game} yet."
    lines = [f"**Backups for {game}** (newest first)"]
    for b in backups[:15]:
        lines.append(f"`#{b.id}` {b.created_at:%Y-%m-%d %H:%M} UTC · {size(b.bytes)} · {b.reason}")
    return "\n".join(lines)
