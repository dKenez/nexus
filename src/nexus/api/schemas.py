from datetime import datetime

from pydantic import BaseModel

from nexus.core.orchestrator import GameView, HostView
from nexus.db.models import Backup, GameStatus, HostStatus


class GameOut(BaseModel):
    name: str
    display_name: str
    enabled: bool
    status: GameStatus
    since: datetime
    started_at: datetime | None
    players: int | None
    last_player_seen_at: datetime | None
    dirty: bool
    last_error: str | None
    pinned_backup_id: int | None
    ports: list[str]

    @classmethod
    def of(cls, view: GameView) -> "GameOut":
        return cls(
            name=view.recipe.name,
            display_name=view.recipe.display_name,
            enabled=view.recipe.enabled,
            status=view.status,
            since=view.since,
            started_at=view.started_at,
            players=view.players,
            last_player_seen_at=view.last_player_seen_at,
            dirty=view.dirty,
            last_error=view.last_error,
            pinned_backup_id=view.pinned_backup_id,
            ports=[str(p) for p in view.recipe.ports],
        )


class HostOut(BaseModel):
    name: str
    status: HostStatus
    hcloud_id: int | None
    server_type: str
    memory_mb: int
    created_at: datetime
    ready_at: datetime | None
    ip: str
    last_error: str | None
    hourly_price: str | None = None

    @classmethod
    def of(cls, view: HostView, price: str | None) -> "HostOut":
        return cls(
            name=view.name,
            status=view.status,
            hcloud_id=view.hcloud_id,
            server_type=view.server_type,
            memory_mb=view.memory_mb,
            created_at=view.created_at,
            ready_at=view.ready_at,
            ip=view.ip,
            last_error=view.last_error,
            hourly_price=price,
        )


class BackupOut(BaseModel):
    id: int
    game: str
    filename: str
    bytes: int
    sha256: str
    reason: str
    created_at: datetime

    @classmethod
    def of(cls, backup: Backup) -> "BackupOut":
        return cls(
            id=backup.id,
            game=backup.game,
            filename=backup.filename,
            bytes=backup.bytes,
            sha256=backup.sha256,
            reason=backup.reason,
            created_at=backup.created_at,
        )


class Accepted(BaseModel):
    accepted: bool = True
    detail: str
