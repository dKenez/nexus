"""Rendering orchestrator state for Discord.

One visual language everywhere, the one ``/games`` uses: an embed titled with the game (or
"Host"), a line of ``<icon> **<status>** · detail · detail``, and a small footer. Colours say
what it means for players: green = up, teal = neutral, orange = warning, red = error.
"""

from datetime import datetime
from zoneinfo import ZoneInfo

import discord

from nexus.core.notify import Event, Tone
from nexus.core.orchestrator import GameView, HostView
from nexus.core.timefmt import clock, date_clock, duration
from nexus.db.models import Backup, GameStatus, HostStatus, Snapshot

STATUS_ICON = {
    GameStatus.STOPPED: "⚫",
    GameStatus.STARTING: "🟡",
    GameStatus.RESTORING: "🟡",
    GameStatus.RUNNING: "🟢",
    GameStatus.STOPPING: "🟠",
    GameStatus.BACKING_UP: "🟠",
    GameStatus.FAILED: "🔴",
}

COLOURS = {
    Tone.GOOD: discord.Colour.green(),
    Tone.NEUTRAL: discord.Colour.dark_teal(),
    Tone.WARNING: discord.Colour.orange(),
    Tone.ERROR: discord.Colour.red(),
}


def size(num: int) -> str:
    value = float(num)
    for unit in ("B", "KiB", "MiB", "GiB"):
        if value < 1024:
            return f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} TiB"


def event_embed(event: Event, footer: str | None = None) -> discord.Embed:
    """A notification, in the same shape as a line of /games."""
    embed = discord.Embed(
        title=event.title, description=event.line()[:4000], colour=COLOURS[event.colour]
    )
    text = "\n".join(t for t in (event.footer, footer) if t)
    if text:
        embed.set_footer(text=text[:2000])
    return embed


def game_line(view: GameView, now: datetime, address: str) -> str:
    parts = [f"{STATUS_ICON[view.status]} **{view.status.replace('_', ' ')}**"]
    if view.status is GameStatus.RUNNING:
        if view.players is not None:
            parts.append(f"{view.players} player{'s' if view.players != 1 else ''}")
        if view.started_at:
            parts.append(f"up {duration(now - view.started_at)}")
        port = view.recipe.ports[0].port
        parts.append(f"`{address}:{port}`")
    line = " · ".join(parts)
    if view.last_error and view.status in (GameStatus.FAILED, GameStatus.STOPPED):
        line += f"\n⚠️ {view.last_error[:200]}"
    return line


def game_embed(view: GameView, now: datetime, address: str) -> discord.Embed:
    tone = Tone.GOOD if view.status is GameStatus.RUNNING else Tone.NEUTRAL
    if view.status is GameStatus.FAILED:
        tone = Tone.ERROR
    return discord.Embed(
        title=view.recipe.display_name,
        description=game_line(view, now, address),
        colour=COLOURS[tone],
    )


def games_embed(
    views: list[GameView], host: HostView | None, now: datetime, address: str
) -> discord.Embed:
    embed = discord.Embed(title="Game servers", colour=COLOURS[Tone.NEUTRAL])
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
    host: HostView | None,
    running: list[GameView],
    price: str | None,
    now: datetime,
    tz: ZoneInfo,
) -> discord.Embed:
    if host is None:
        return discord.Embed(
            title="Host",
            description="⚫ **no VM** · nothing is being billed",
            colour=COLOURS[Tone.NEUTRAL],
        )
    ready = host.status is HostStatus.READY
    icon = "🟢" if ready and running else ("⚫" if ready else "🟠")
    parts = [
        f"{icon} **{'empty' if ready and not running else host.status}**",
        f"{host.server_type} ({host.memory_mb // 1024} GB)",
        f"up {duration(now - host.created_at)}",
    ]
    if price:
        parts.append(f"€{float(price):.4f}/h")
    embed = discord.Embed(
        title="Host",
        description=" · ".join(parts),
        colour=COLOURS[Tone.GOOD if ready and running else Tone.NEUTRAL],
    )
    embed.add_field(
        name="Games",
        value=", ".join(v.recipe.display_name for v in running) or "none",
        inline=False,
    )
    if host.last_error:
        embed.add_field(name="Last error", value=host.last_error[:500], inline=False)
    footer = [host.name, f"paid until {clock(host.paid_until, tz)}"]
    if host.delete_at is not None:
        footer.append(f"deleted at {clock(host.delete_at, tz)} unless a game starts")
    embed.set_footer(text=" · ".join(footer))
    return embed


def backups_embed(
    title: str, backups: list[Backup], snapshots: list[Snapshot], tz: ZoneInfo
) -> discord.Embed:
    embed = discord.Embed(title=f"{title} backups", colour=COLOURS[Tone.NEUTRAL])
    full = [
        f"`#{b.id}` {date_clock(b.created_at, tz)} · {size(b.bytes)} · {b.reason}"
        for b in backups[:12]
    ]
    embed.add_field(
        name="Full backups (taken on stop)", value="\n".join(full) or "none yet", inline=False
    )
    if snapshots:
        lines = [
            f"{date_clock(sn.taken_at, tz)} · {size(sn.bytes)} · `{sn.filename}`"
            for sn in snapshots[:8]
        ]
        embed.add_field(
            name="Snapshots (copied while running)", value="\n".join(lines), inline=False
        )
    embed.set_footer(text="Newest first · /game restore to start from one")
    return embed
