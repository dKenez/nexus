import asyncio
import io
import shutil
import subprocess
import tarfile
import zipfile
from pathlib import Path

import httpx
import pytest
import zstandard

from nexus.api.app import build_api
from nexus.core.importer import ArchiveError, normalize_archive
from nexus.core.orchestrator import InvalidStateError
from nexus.core.recipes import ImportSpec
from nexus.core.tasks import TaskRunner
from nexus.db.models import GameStatus
from tests.conftest import World

SPEC = ImportSpec(required=("worlds_local/{WORLD_NAME}*",))
EXCLUDE = ("backups", "worlds_local/*_backup_auto-*")
ENV = {"WORLD_NAME": "VoE"}

# What `docker cp valheim:/config -` of the prod server contains (Valheim 1.0 world layout).
CONFIG = {
    "worlds_local/VoE/_main.1.db2": b"world",
    "worlds_local/VoE/_main.1.ok": b"ok",
    "worlds_local/VoE/1e_20__1_1.chunk": b"chunk",
    "adminlist.txt": b"// admins\n",
    "backups/worlds-20260925-120500.zip": b"old snapshot",
    "worlds_local/VoE_backup_auto-20260925-021039/_main.1.db2": b"valheim's own copy",
}


def make_tar(path: Path, files: dict[str, bytes], prefix: str = "", mode: str = "w") -> Path:
    with tarfile.open(path, mode) as tar:  # ty: ignore[no-matching-overload]
        for name, data in files.items():
            info = tarfile.TarInfo(prefix + name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    return path


def read_tar_zst(path: Path) -> dict[str, bytes]:
    with (
        path.open("rb") as f,
        zstandard.ZstdDecompressor().stream_reader(f) as z,
        tarfile.open(fileobj=z, mode="r|") as tar,
    ):
        return {
            m.name: tar.extractfile(m).read()  # ty: ignore[unresolved-attribute]
            for m in tar
            if m.isfile()
        }


def normalize(tmp_path: Path, source: Path, spec: ImportSpec = SPEC):
    dest = tmp_path / "out.tar.zst"
    report = normalize_archive(source, dest, spec, ENV, exclude=EXCLUDE, scratch=tmp_path)
    return report, read_tar_zst(dest)


EXPECTED = {
    k: v for k, v in CONFIG.items() if not k.startswith(("backups/", "worlds_local/VoE_backup"))
}


def test_docker_cp_layout_is_unwrapped_and_cleaned(tmp_path: Path) -> None:
    source = make_tar(tmp_path / "config.tar", CONFIG, prefix="config/")
    report, content = normalize(tmp_path, source)
    assert content == EXPECTED
    assert report.stripped == "config"
    assert report.excluded == ["backups", "worlds_local/*_backup_auto-*"]
    assert report.files == 4


def test_tar_of_the_directory_itself(tmp_path: Path) -> None:
    source = make_tar(tmp_path / "config.tar.gz", CONFIG, prefix="./", mode="w:gz")
    report, content = normalize(tmp_path, source)
    assert content == EXPECTED
    assert report.stripped is None


def test_tar_zst_and_zip(tmp_path: Path) -> None:
    plain = make_tar(tmp_path / "plain.tar", CONFIG)
    zst = tmp_path / "config.tar.zst"
    with plain.open("rb") as src, zst.open("wb") as dst:
        zstandard.ZstdCompressor().copy_stream(src, dst)
    assert normalize(tmp_path, zst)[1] == EXPECTED

    zipped = tmp_path / "config.zip"
    with zipfile.ZipFile(zipped, "w") as z:
        for name, data in CONFIG.items():
            z.writestr("config/" + name, data)
    assert normalize(tmp_path, zipped)[1] == EXPECTED


def test_pre_1_0_world_files_are_accepted(tmp_path: Path) -> None:
    old = {"worlds_local/VoE.db": b"db", "worlds_local/VoE.fwl": b"fwl"}
    assert normalize(tmp_path, make_tar(tmp_path / "old.tar", old))[1] == old


def test_wrong_world_is_refused(tmp_path: Path) -> None:
    other = {"worlds_local/Dedicated/_main.1.db2": b"x", "adminlist.txt": b""}
    with pytest.raises(ArchiveError, match="missing worlds_local/VoE"):
        normalize(tmp_path, make_tar(tmp_path / "other.tar", other, prefix="config/"))


def test_path_traversal_is_refused(tmp_path: Path) -> None:
    evil = {"../../etc/cron.d/x": b"boom", "worlds_local/VoE/_main.1.db2": b"x"}
    with pytest.raises(ArchiveError, match="escapes"):
        normalize(tmp_path, make_tar(tmp_path / "evil.tar", evil))


def test_links_are_skipped(tmp_path: Path) -> None:
    source = tmp_path / "links.tar"
    with tarfile.open(source, "w") as tar:
        data = b"world"
        info = tarfile.TarInfo("worlds_local/VoE/_main.1.db2")
        info.size = len(data)
        tar.addfile(info, io.BytesIO(data))
        link = tarfile.TarInfo("worlds_local/escape")
        link.type = tarfile.SYMTYPE
        link.linkname = "/etc/passwd"
        tar.addfile(link)
    report, content = normalize(tmp_path, source)
    assert report.skipped == ["worlds_local/escape"]
    assert list(content) == ["worlds_local/VoE/_main.1.db2"]


def test_garbage_is_refused(tmp_path: Path) -> None:
    junk = tmp_path / "junk.bin"
    junk.write_bytes(b"definitely not an archive" * 100)
    with pytest.raises(ArchiveError, match="not a tar"):
        normalize(tmp_path, junk)


# --- orchestrator + API ---


def use_import_spec(world: World) -> None:
    recipes = world.orch.recipes._recipes
    recipes["alpha"] = recipes["alpha"].model_copy(
        update={"import_": SPEC, "env": {"WORLD_NAME": "VoE"}, "backup_exclude": EXCLUDE}
    )


async def upload(world: World, source: Path) -> Path:
    async def chunks():
        yield source.read_bytes()

    return await world.orch.receive_upload(chunks())


async def test_import_becomes_the_restored_world(world: World, tmp_path: Path) -> None:
    use_import_spec(world)
    await world.orch.start("alpha")
    await world.orch.stop("alpha")  # an older backup exists...
    first = (await world.orch.backups("alpha"))[0]
    await world.orch.pin_backup("alpha", first.id)  # ...and is even pinned
    world.clock.advance(minutes=1)

    source = make_tar(tmp_path / "config.tar", CONFIG, prefix="config/")
    backup, report = await world.orch.import_backup("alpha", await upload(world, source))
    assert backup.reason == "import"
    assert report.files == 4
    assert (await world.orch.game("alpha")).pinned_backup_id is None
    assert list(world.backups.incoming_dir.iterdir()) == []  # scratch files cleaned up

    await world.orch.start("alpha")
    restored = world.agents.agent.data["alpha"].removesuffix(b"+played")
    stored = world.backups.path("alpha", backup.filename).read_bytes()
    assert restored == stored
    assert read_tar_zst(world.backups.path("alpha", backup.filename)) == EXPECTED


async def test_import_requires_a_stopped_game(world: World, tmp_path: Path) -> None:
    use_import_spec(world)
    await world.orch.start("alpha")
    source = make_tar(tmp_path / "config.tar", CONFIG)
    path = await upload(world, source)
    with pytest.raises(InvalidStateError, match="stop Alpha before importing"):
        await world.orch.import_backup("alpha", path)
    assert not path.exists()


async def test_api_import(world: World, tmp_path: Path) -> None:
    use_import_spec(world)
    api = build_api()
    api.state.api_token = "t" * 32
    api.state.orchestrator = world.orch
    api.state.sessions = world.sessions
    api.state.tasks = TaskRunner(world.notifier)
    client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=api),
        base_url="http://test",
        headers={"X-API-KEY": "t" * 32},
    )
    good = make_tar(tmp_path / "config.tar", CONFIG, prefix="config/").read_bytes()
    response = await client.post("/api/games/alpha/import", content=good)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["files"] == 4 and body["stripped"] == "config"
    assert body["excluded"] == ["backups", "worlds_local/*_backup_auto-*"]

    bad = make_tar(tmp_path / "bad.tar", {"something/else.txt": b"x"}).read_bytes()
    response = await client.post("/api/games/alpha/import", content=bad)
    assert response.status_code == 422
    assert "missing worlds_local/VoE" in response.json()["detail"]
    assert world.orch.operation("alpha") is None

    await world.orch.start("alpha")
    response = await client.post("/api/games/alpha/import", content=good)
    assert response.status_code == 409
    assert (await world.orch.game("alpha")).status is GameStatus.RUNNING
    await asyncio.sleep(0)


# --- exclusion rules: identical in Python (imports) and GNU tar (stop backups) ---

TREE = [
    "adminlist.txt",
    "backups/worlds-1.zip",
    "worlds_local/VoE/a.db",
    "worlds_local/VoE/backups/keep",  # only the top-level backups/ is excluded
    "worlds_local/VoE/nested_backup_auto-2",  # * doesn't cross /
    "worlds_local/VoE_backup_auto-1/x",
]
KEPT = {
    "adminlist.txt",
    "worlds_local/VoE/a.db",
    "worlds_local/VoE/backups/keep",
    "worlds_local/VoE/nested_backup_auto-2",
}


def test_excluded_rules() -> None:
    from nexus.core.importer import excluded

    assert {n for n in TREE if excluded(n, EXCLUDE) is None} == KEPT


@pytest.mark.skipif(
    not shutil.which("tar")
    or "GNU" not in subprocess.run(["tar", "--version"], capture_output=True, text=True).stdout,
    reason="needs GNU tar",
)
def test_vm_tar_command_excludes_the_same_files(tmp_path: Path) -> None:
    from nexus.infra.agent import archive_command, data_dir

    for name in TREE:
        (tmp_path / name).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / name).write_bytes(b"x")
    command = (
        archive_command("g", EXCLUDE).replace("sudo ", "").replace(data_dir("g"), str(tmp_path))
    )
    listing = subprocess.run(
        f"{command} | tar -tf -", shell=True, capture_output=True, text=True, check=True
    ).stdout
    files = {line.removeprefix("./") for line in listing.splitlines() if not line.endswith("/")}
    assert files == KEPT


# --- restoring a snapshot ---


def make_backup(path: Path, files: dict[str, bytes]) -> Path:
    plain = make_tar(path.with_suffix(".plain"), files)
    with plain.open("rb") as src, path.open("wb") as dst:
        zstandard.ZstdCompressor().copy_stream(src, dst)
    return path


def make_snapshot(path: Path, files: dict[str, bytes]) -> Path:
    # Laid out like lloesche/valheim-server's `zip -r worlds-*.zip worlds_local/`.
    with zipfile.ZipFile(path, "w") as z:
        for name, data in files.items():
            z.writestr(name, data)
    return path


BASE = {
    "adminlist.txt": b"admins",
    "prefs": b"prefs",
    "worlds_local/VoE/_main.1.db2": b"old world",
    "worlds_local/VoE/gone.chunk": b"deleted since",
}
SNAPSHOT = {
    "worlds_local/VoE/_main.2.db2": b"new world",
    "worlds_local/VoE/_main.2.ok": b"ok",
}


def test_snapshot_replaces_the_world_and_keeps_the_rest(tmp_path: Path) -> None:
    from nexus.core.importer import compose_snapshot

    dest = tmp_path / "out.tar.zst"
    compose_snapshot(
        make_backup(tmp_path / "base.tar.zst", BASE),
        make_snapshot(tmp_path / "worlds-1.zip", SNAPSHOT),
        dest,
        SPEC,
        ENV,
        exclude=EXCLUDE,
        scratch=tmp_path,
    )
    assert read_tar_zst(dest) == {"adminlist.txt": b"admins", "prefs": b"prefs", **SNAPSHOT}


def test_snapshot_without_a_full_backup(tmp_path: Path) -> None:
    from nexus.core.importer import compose_snapshot

    dest = tmp_path / "out.tar.zst"
    compose_snapshot(
        None,
        make_snapshot(tmp_path / "worlds-1.zip", SNAPSHOT),
        dest,
        SPEC,
        ENV,
        scratch=tmp_path,
    )
    assert read_tar_zst(dest) == SNAPSHOT


def test_snapshot_of_another_world_is_refused(tmp_path: Path) -> None:
    from nexus.core.importer import compose_snapshot

    other = make_snapshot(tmp_path / "worlds-1.zip", {"worlds_local/Dedicated/x": b"x"})
    with pytest.raises(ArchiveError, match="missing worlds_local/VoE"):
        compose_snapshot(
            make_backup(tmp_path / "base.tar.zst", {"adminlist.txt": b""}),
            other,
            tmp_path / "out.tar.zst",
            SPEC,
            ENV,
            scratch=tmp_path,
        )


async def test_restore_snapshot_end_to_end(world: World, tmp_path: Path) -> None:
    from nexus.core.recipes import Snapshots

    use_import_spec(world)
    recipes = world.orch.recipes._recipes
    recipes["alpha"] = recipes["alpha"].model_copy(
        update={"snapshots": Snapshots(dir="backups", pattern="worlds-*.zip", settle_seconds=0)}
    )
    # A world on ymir, then a session during which the image writes an hourly zip.
    source = make_tar(tmp_path / "config.tar", {**BASE, "worlds_local/VoE/_main.1.ok": b""})
    await world.orch.import_backup("alpha", await upload(world, source))
    await world.orch.start("alpha")
    agent = world.agents.agent
    agent.remote_now = 1_790_000_000.0
    zip_bytes = make_snapshot(tmp_path / "worlds-2.zip", SNAPSHOT).read_bytes()
    agent.files["alpha"] = {
        "backups/worlds-20260926-010500.zip": (zip_bytes, agent.remote_now - 60),
        "backups/unrelated.log": (b"noise", agent.remote_now - 30),
    }
    assert await world.orch.pull_snapshots() == ["worlds-20260926-010500.zip"]
    snapshot = (await world.orch.snapshots("alpha"))[0]

    with pytest.raises(InvalidStateError, match="stop Alpha"):
        await world.orch.restore_snapshot("alpha", snapshot.id)
    # The fake server's "+played" marker isn't a real archive; hand the stop backup a
    # genuine one (on a real VM, the stop backup is a tar.zst of the data directory).
    imported = (await world.orch.backups("alpha"))[0]
    agent.data["alpha"] = world.backups.path("alpha", imported.filename).read_bytes()
    await world.orch.stop("alpha")
    world.clock.advance(minutes=1)

    backup = await world.orch.restore_snapshot("alpha", snapshot.id)
    assert backup.reason == "snapshot"
    restored = read_tar_zst(world.backups.path("alpha", backup.filename))
    assert restored == {"adminlist.txt": b"admins", "prefs": b"prefs", **SNAPSHOT}

    await world.orch.start("alpha")
    on_vm = world.agents.agent.data["alpha"].removesuffix(b"+played")
    assert on_vm == world.backups.path("alpha", backup.filename).read_bytes()
