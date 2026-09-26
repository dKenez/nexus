"""Backup archives on local storage (ymir's tank, via the pod's PVC)."""

import asyncio
import hashlib
import io
import os
import re
import shutil
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from nexus.infra.agent import CHUNK, Sink

FILENAME_RE = re.compile(r"^\d{8}T\d{6}Z-[a-z]+\.tar\.zst$")
SNAPSHOT_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


@dataclass(frozen=True)
class WrittenBackup:
    filename: str
    bytes: int
    sha256: str


class BackupError(Exception):
    pass


class BackupStore:
    def __init__(self, root: Path) -> None:
        self.root = root

    def game_dir(self, game: str) -> Path:
        return self.root / game

    def path(self, game: str, filename: str) -> Path:
        if not FILENAME_RE.match(filename):
            raise BackupError(f"invalid backup filename {filename!r}")
        return self.game_dir(game) / filename

    @property
    def incoming_dir(self) -> Path:
        """Uploads and import scratch space. On the same filesystem as the backups, so an
        imported archive is moved into place, not copied, and never lands in a small /tmp."""
        return self.root / ".incoming"

    async def clear_incoming(self) -> None:
        """Remove leftovers of interrupted imports (only one nexus runs, so all are stale)."""
        await asyncio.to_thread(shutil.rmtree, self.incoming_dir, ignore_errors=True)
        await asyncio.to_thread(self.incoming_dir.mkdir, parents=True, exist_ok=True)

    def scratch_path(self, suffix: str) -> Path:
        return self.incoming_dir / f"{uuid.uuid4().hex}{suffix}"

    async def receive(self, chunks: AsyncIterator[bytes]) -> Path:
        """Store an uploaded file in the incoming directory and return its path."""
        path = self.scratch_path(".upload")
        await asyncio.to_thread(path.parent.mkdir, parents=True, exist_ok=True)
        f = await asyncio.to_thread(_open_write, path)
        try:
            async for chunk in chunks:
                await asyncio.to_thread(f.write, chunk)
        except BaseException:
            f.close()
            path.unlink(missing_ok=True)
            raise
        f.close()
        return path

    async def adopt(self, game: str, reason: str, source: Path, *, now: datetime) -> WrittenBackup:
        """Move a finished archive into place as a backup (hash, fsync, rename)."""
        filename = f"{now:%Y%m%dT%H%M%SZ}-{reason}.tar.zst"
        final = self.path(game, filename)

        def adopt() -> WrittenBackup:
            digest = hashlib.sha256()
            with source.open("rb") as f:
                while chunk := f.read(CHUNK):
                    digest.update(chunk)
                os.fsync(f.fileno())
            final.parent.mkdir(parents=True, exist_ok=True)
            source.rename(final)
            return WrittenBackup(
                filename=filename, bytes=final.stat().st_size, sha256=digest.hexdigest()
            )

        return await asyncio.to_thread(adopt)

    async def check_writable(self) -> None:
        def check() -> None:
            self.root.mkdir(parents=True, exist_ok=True)
            probe = self.root / ".write-probe"
            probe.write_bytes(b"ok")
            probe.unlink()

        await asyncio.to_thread(check)

    async def write(
        self,
        game: str,
        reason: str,
        produce: Callable[[Sink], Awaitable[None]],
        *,
        now: datetime,
    ) -> WrittenBackup:
        """Write a full archive of a game's data directory."""
        filename = f"{now:%Y%m%dT%H%M%SZ}-{reason}.tar.zst"
        return await self._write_atomic(self.path(game, filename), produce)

    def snapshot_path(self, game: str, name: str) -> Path:
        if not SNAPSHOT_NAME_RE.match(name):
            raise BackupError(f"invalid snapshot filename {name!r}")
        return self.game_dir(game) / "snapshots" / name

    async def write_snapshot(
        self, game: str, name: str, produce: Callable[[Sink], Awaitable[None]]
    ) -> WrittenBackup:
        """Store a snapshot file (made by the game image itself) under its own name."""
        return await self._write_atomic(self.snapshot_path(game, name), produce)

    async def delete_snapshot(self, game: str, name: str) -> None:
        await asyncio.to_thread(self.snapshot_path(game, name).unlink, missing_ok=True)

    async def _write_atomic(
        self, final: Path, produce: Callable[[Sink], Awaitable[None]]
    ) -> WrittenBackup:
        """Stream to ``.partial``, fsync, then rename, so a file under its final name is whole."""
        partial = final.with_name(final.name + ".partial")
        await asyncio.to_thread(final.parent.mkdir, parents=True, exist_ok=True)

        digest = hashlib.sha256()
        size = 0
        f = await asyncio.to_thread(_open_write, partial)
        try:

            async def sink(chunk: bytes) -> None:
                nonlocal size
                digest.update(chunk)
                size += len(chunk)
                await asyncio.to_thread(f.write, chunk)

            await produce(sink)
            await asyncio.to_thread(f.flush)
            await asyncio.to_thread(_fsync, f.fileno())
        except BaseException:
            f.close()
            partial.unlink(missing_ok=True)
            raise
        f.close()
        if size == 0:
            partial.unlink(missing_ok=True)
            raise BackupError(f"{final.name} would be empty")
        await asyncio.to_thread(partial.rename, final)
        return WrittenBackup(filename=final.name, bytes=size, sha256=digest.hexdigest())

    async def read(self, game: str, filename: str) -> AsyncIterator[bytes]:
        path = self.path(game, filename)
        f = await asyncio.to_thread(_open_read, path)
        try:
            while chunk := await asyncio.to_thread(f.read, CHUNK):
                yield chunk
        finally:
            f.close()

    async def exists(self, game: str, filename: str) -> bool:
        return await asyncio.to_thread(self.path(game, filename).is_file)

    async def delete(self, game: str, filename: str) -> None:
        await asyncio.to_thread(self.path(game, filename).unlink, missing_ok=True)


def _open_write(path: Path) -> io.BufferedWriter:
    return path.open("wb")


def _open_read(path: Path) -> io.BufferedReader:
    return path.open("rb")


def _fsync(fd: int) -> None:
    os.fsync(fd)
