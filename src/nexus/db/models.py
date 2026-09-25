"""Persistent runtime state. Recipes live on disk; only what changes at runtime is stored here."""

from datetime import UTC, datetime
from enum import StrEnum
from typing import ClassVar

from sqlalchemy import BigInteger, DateTime, Dialect, Enum, ForeignKey, Integer, String, Text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy.types import TypeDecorator


def utcnow() -> datetime:
    return datetime.now(UTC)


class UTCDateTime(TypeDecorator[datetime]):
    """Timezone-aware UTC datetimes on every backend (SQLite drops the offset)."""

    impl = DateTime(timezone=True)
    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect: Dialect) -> datetime | None:
        if value is not None and value.tzinfo is None:
            raise ValueError("naive datetime")
        return value

    def process_result_value(self, value: datetime | None, dialect: Dialect) -> datetime | None:
        if value is not None and value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value


class Base(DeclarativeBase):
    type_annotation_map: ClassVar = {datetime: UTCDateTime()}


class HostStatus(StrEnum):
    PROVISIONING = "provisioning"
    READY = "ready"
    DELETING = "deleting"
    DELETED = "deleted"
    FAILED = "failed"


class GameStatus(StrEnum):
    STOPPED = "stopped"
    STARTING = "starting"
    RESTORING = "restoring"
    RUNNING = "running"
    STOPPING = "stopping"
    BACKING_UP = "backing_up"
    FAILED = "failed"

    @property
    def occupies_host(self) -> bool:
        """Whether a game in this state has (or is about to have) data on the host."""
        return self is not GameStatus.STOPPED


class Host(Base):
    __tablename__ = "hosts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    hcloud_id: Mapped[int | None] = mapped_column(BigInteger, unique=True)
    name: Mapped[str] = mapped_column(String(64))
    status: Mapped[HostStatus] = mapped_column(Enum(HostStatus, native_enum=False, length=16))
    server_type: Mapped[str] = mapped_column(String(32))
    memory_mb: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    ready_at: Mapped[datetime | None]
    deleted_at: Mapped[datetime | None]
    # When the host last became empty (no games occupying it); drives host GC.
    empty_since: Mapped[datetime | None]
    last_error: Mapped[str | None] = mapped_column(Text)


class GameState(Base):
    __tablename__ = "game_state"

    game: Mapped[str] = mapped_column(String(32), primary_key=True)
    status: Mapped[GameStatus] = mapped_column(
        Enum(GameStatus, native_enum=False, length=16), default=GameStatus.STOPPED
    )
    since: Mapped[datetime] = mapped_column(default=utcnow)
    host_id: Mapped[int | None] = mapped_column(ForeignKey("hosts.id"))
    started_at: Mapped[datetime | None]
    last_player_count: Mapped[int | None]
    last_player_seen_at: Mapped[datetime | None]
    # The data on the host differs from the newest backup; the host must not be deleted.
    dirty: Mapped[bool] = mapped_column(default=False)
    pinned_backup_id: Mapped[int | None] = mapped_column(ForeignKey("backups.id"))
    last_error: Mapped[str | None] = mapped_column(Text)


class Backup(Base):
    __tablename__ = "backups"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    game: Mapped[str] = mapped_column(String(32), index=True)
    filename: Mapped[str] = mapped_column(String(128))
    bytes: Mapped[int] = mapped_column(BigInteger)
    sha256: Mapped[str] = mapped_column(String(64))
    reason: Mapped[str] = mapped_column(String(32))
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    deleted_at: Mapped[datetime | None]


class AuditLog(Base):
    __tablename__ = "audit_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    at: Mapped[datetime] = mapped_column(default=utcnow)
    actor: Mapped[str] = mapped_column(String(64))
    action: Mapped[str] = mapped_column(String(32))
    target: Mapped[str | None] = mapped_column(String(64))
    result: Mapped[str] = mapped_column(String(16))
    detail: Mapped[str | None] = mapped_column(Text)
