import asyncio
import io
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

SPEC = ImportSpec(required=("worlds_local/{WORLD_NAME}*",), exclude=("backups",))
ENV = {"WORLD_NAME": "VoE"}

# What `docker cp valheim:/config -` of the prod server contains (Valheim 1.0 world layout).
CONFIG = {
    "worlds_local/VoE/_main.1.db2": b"world",
    "worlds_local/VoE/_main.1.ok": b"ok",
    "worlds_local/VoE/1e_20__1_1.chunk": b"chunk",
    "adminlist.txt": b"// admins\n",
    "backups/worlds_local-20260925-120500.zip": b"old snapshot",
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
    report = normalize_archive(source, dest, spec, ENV, scratch=tmp_path)
    return report, read_tar_zst(dest)


EXPECTED = {k: v for k, v in CONFIG.items() if not k.startswith("backups/")}


def test_docker_cp_layout_is_unwrapped_and_cleaned(tmp_path: Path) -> None:
    source = make_tar(tmp_path / "config.tar", CONFIG, prefix="config/")
    report, content = normalize(tmp_path, source)
    assert content == EXPECTED
    assert report.stripped == "config"
    assert report.excluded == ["backups"]
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
        update={"import_": SPEC, "env": {"WORLD_NAME": "VoE"}}
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
    assert body["files"] == 4 and body["stripped"] == "config" and body["excluded"] == ["backups"]

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
