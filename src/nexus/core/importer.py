"""Turn an uploaded archive into a nexus backup of a game's data directory.

Accepts .tar, .tar.gz/.bz2/.xz, .tar.zst and .zip. The contents are checked against the
recipe's ``[import]`` spec and rewritten as the same ``tar.zst`` nexus produces on stop, so an
import is restored exactly like any other backup. Runs synchronously; call it in a thread.
"""

import datetime
import fnmatch
import tarfile
import zipfile
from collections.abc import Iterator
from contextlib import ExitStack
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import BinaryIO

import zstandard

from nexus.core.recipes import ImportSpec

ZSTD_MAGIC = b"\x28\xb5\x2f\xfd"
ZIP_MAGIC = b"PK\x03\x04"


class ArchiveError(Exception):
    """The archive is unreadable, unsafe, or not a data directory for this game."""


@dataclass(frozen=True)
class ImportReport:
    files: int
    bytes: int
    stripped: str | None
    excluded: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class _Entry:
    name: str  # normalised relative POSIX path, no leading "./"
    is_dir: bool
    size: int
    mtime: float
    mode: int
    uid: int
    gid: int
    key: object  # tar member or zip info, to read the data back


def _normalise(raw: str) -> str | None:
    """A safe relative path, or None for the archive root. Raises on escaping paths."""
    path = PurePosixPath(raw.replace("\\", "/"))
    if path.is_absolute():
        raise ArchiveError(f"archive contains an absolute path: {raw!r}")
    parts = [p for p in path.parts if p not in ("", ".")]
    if ".." in parts:
        raise ArchiveError(f"archive contains a path that escapes it: {raw!r}")
    return "/".join(parts) or None


class _Reader:
    """Uniform access to the entries of a tar or zip archive."""

    def __init__(self, source: Path, scratch: Path, stack: ExitStack) -> None:
        with source.open("rb") as f:
            magic = f.read(4)
        self.skipped: list[str] = []
        if magic == ZSTD_MAGIC:
            # Python 3.13's tarfile can't read zstd; decompress to a plain tar first.
            plain = scratch / (source.name + ".tar")
            with source.open("rb") as src, plain.open("wb") as dst:
                zstandard.ZstdDecompressor().copy_stream(src, dst)
            stack.callback(plain.unlink, missing_ok=True)
            source = plain
        if magic == ZIP_MAGIC:
            self._zip: zipfile.ZipFile | None = stack.enter_context(zipfile.ZipFile(source))
            self._tar: tarfile.TarFile | None = None
        else:
            try:
                self._tar = stack.enter_context(tarfile.open(source, "r:*"))  # noqa: SIM115
            except tarfile.TarError as exc:
                raise ArchiveError(f"not a tar, tar.gz, tar.zst or zip archive: {exc}") from exc
            self._zip = None
        self.entries = list(self._scan())

    def _scan(self) -> Iterator[_Entry]:
        if self._zip is not None:
            for info in self._zip.infolist():
                name = _normalise(info.filename)
                if name is None:
                    continue
                mode = (info.external_attr >> 16) & 0o7777
                is_link = ((info.external_attr >> 16) & 0o170000) == 0o120000
                if is_link:
                    self.skipped.append(name)
                    continue
                yield _Entry(
                    name=name,
                    is_dir=info.is_dir(),
                    size=info.file_size,
                    mtime=_zip_mtime(info),
                    mode=mode or (0o755 if info.is_dir() else 0o644),
                    uid=0,
                    gid=0,
                    key=info,
                )
            return
        assert self._tar is not None
        for member in self._tar:
            name = _normalise(member.name)
            if name is None:
                continue
            if not (member.isfile() or member.isdir()):
                self.skipped.append(name)  # links, devices, fifos
                continue
            yield _Entry(
                name=name,
                is_dir=member.isdir(),
                size=member.size if member.isfile() else 0,
                mtime=member.mtime,
                mode=member.mode,
                uid=member.uid,
                gid=member.gid,
                key=member,
            )

    def open(self, entry: _Entry) -> BinaryIO:
        if self._zip is not None:
            return self._zip.open(entry.key)  # ty: ignore[invalid-argument-type, invalid-return-type]
        assert self._tar is not None
        f = self._tar.extractfile(entry.key)  # ty: ignore[invalid-argument-type]
        assert f is not None
        return f  # ty: ignore[invalid-return-type]


def _zip_mtime(info: zipfile.ZipInfo) -> float:
    return datetime.datetime(*info.date_time).timestamp()


def _all_paths(names: list[str]) -> set[str]:
    """Every path in the archive, including parent directories that have no entry of their own."""
    paths = set()
    for name in names:
        parts = name.split("/")
        for i in range(1, len(parts) + 1):
            paths.add("/".join(parts[:i]))
    return paths


def _missing(patterns: list[str], names: list[str]) -> list[str]:
    paths = _all_paths(names)
    return [p for p in patterns if not any(fnmatch.fnmatchcase(path, p) for path in paths)]


def _strip(name: str, prefix: str | None) -> str | None:
    if prefix is None:
        return name
    if name == prefix:
        return None
    return name.removeprefix(prefix + "/")


def normalize_archive(
    source: Path,
    dest: Path,
    spec: ImportSpec,
    env: dict[str, str],
    *,
    scratch: Path,
) -> ImportReport:
    """Validate ``source`` and write it to ``dest`` as a data-directory ``tar.zst``."""
    try:
        required = [pattern.format(**env) for pattern in spec.required]
    except KeyError as exc:
        raise ArchiveError(f"import pattern refers to unknown env value {exc}") from None

    with ExitStack() as stack:
        reader = _Reader(source, scratch, stack)
        names = [e.name for e in reader.entries]
        if not names:
            raise ArchiveError("the archive is empty")

        # Archives made from the directory itself (tar -C config .) are used as they are; ones
        # made from its parent (docker cp, tar config/) have one wrapping directory to strip.
        prefix: str | None = None
        missing = _missing(required, names)
        tops = {n.split("/")[0] for n in names}
        if missing and len(tops) == 1:
            candidate = next(iter(tops))
            stripped = [s for n in names if (s := _strip(n, candidate))]
            if not _missing(required, stripped):
                prefix, missing = candidate, []
        if missing:
            found = ", ".join(sorted(tops)[:10])
            raise ArchiveError(
                f"the archive doesn't look like this game's data: missing {', '.join(missing)} "
                f"(top level has: {found})"
            )

        excluded: set[str] = set()
        kept: list[tuple[str, _Entry]] = []
        for entry in reader.entries:
            name = _strip(entry.name, prefix)
            if name is None:
                continue
            top = name.split("/")[0]
            if top in spec.exclude:
                excluded.add(top)
                continue
            kept.append((name, entry))
        if not any(not e.is_dir for _, e in kept):
            raise ArchiveError("the archive contains no files after exclusions")

        total = 0
        with dest.open("wb") as raw:
            compressor = zstandard.ZstdCompressor(level=3, threads=-1)
            with (
                compressor.stream_writer(raw, closefd=False) as zout,
                tarfile.open(fileobj=zout, mode="w|", format=tarfile.PAX_FORMAT) as out,
            ):
                for name, entry in kept:
                    info = tarfile.TarInfo(name)
                    info.mtime = entry.mtime
                    info.mode = entry.mode
                    info.uid, info.gid = entry.uid, entry.gid
                    if entry.is_dir:
                        info.type = tarfile.DIRTYPE
                        out.addfile(info)
                        continue
                    info.size = entry.size
                    with reader.open(entry) as data:
                        out.addfile(info, data)
                    total += entry.size
            raw.flush()

    return ImportReport(
        files=sum(1 for _, e in kept if not e.is_dir),
        bytes=total,
        stripped=prefix,
        excluded=sorted(excluded),
        skipped=sorted(reader.skipped),
    )
