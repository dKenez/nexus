"""Admin HTTP API. Long operations are accepted (202) and run in the background."""

from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request, status

from nexus.api.schemas import Accepted, BackupOut, GameOut, HostOut, SnapshotOut
from nexus.core.orchestrator import InvalidStateError, Orchestrator
from nexus.core.tasks import TaskRunner
from nexus.db.models import GameStatus

router = APIRouter()

ACTOR = "api"


def orchestrator(request: Request) -> Orchestrator:
    return request.app.state.orchestrator


def runner(request: Request) -> TaskRunner:
    return request.app.state.tasks


Orch = Annotated[Orchestrator, Depends(orchestrator)]
Runner = Annotated[TaskRunner, Depends(runner)]


async def _audited(orch: Orchestrator, action: str, target: str | None, coro) -> None:
    try:
        await coro
    except Exception as exc:
        await orch.audit(ACTOR, action, target, "error", str(exc))
        raise
    await orch.audit(ACTOR, action, target, "ok")


@router.get("/games", response_model=list[GameOut], tags=["games"])
async def list_games(orch: Orch) -> list[GameOut]:
    return [GameOut.of(v) for v in await orch.games()]


@router.get("/games/{game}", response_model=GameOut, tags=["games"])
async def get_game(game: str, orch: Orch) -> GameOut:
    return GameOut.of(await orch.game(game))


@router.post(
    "/games/{game}/start",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=Accepted,
    tags=["games"],
)
async def start_game(game: str, orch: Orch, tasks: Runner) -> Accepted:
    op = orch.begin(game, "starting")  # a duplicate request fails here with 409
    try:
        view = await orch.game(game)
        if view.status not in (GameStatus.STOPPED, GameStatus.FAILED):
            raise InvalidStateError(f"{view.recipe.display_name} is {view.status}")
    except BaseException:
        op.release()
        raise
    tasks.spawn(f"start {game}", _audited(orch, "start", game, orch.start(game, op=op)))
    return Accepted(detail=f"starting {game}")


@router.post(
    "/games/{game}/stop",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=Accepted,
    tags=["games"],
)
async def stop_game(game: str, orch: Orch, tasks: Runner) -> Accepted:
    op = orch.begin(game, "stopping")
    try:
        view = await orch.game(game)
        if view.status is GameStatus.STOPPED:
            raise InvalidStateError(f"{view.recipe.display_name} is not running")
    except BaseException:
        op.release()
        raise
    tasks.spawn(f"stop {game}", _audited(orch, "stop", game, orch.stop(game, op=op)))
    return Accepted(detail=f"stopping {game}")


@router.post(
    "/games/{game}/backup",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=Accepted,
    tags=["games"],
)
async def backup_game(game: str, orch: Orch, tasks: Runner) -> Accepted:
    op = orch.begin(game, "being backed up")
    tasks.spawn(f"backup {game}", _audited(orch, "backup", game, orch.backup(game, op=op)))
    return Accepted(detail=f"backing up {game}")


@router.get("/games/{game}/backups", response_model=list[BackupOut], tags=["games"])
async def list_backups(game: str, orch: Orch) -> list[BackupOut]:
    return [BackupOut.of(b) for b in await orch.backups(game)]


@router.get("/games/{game}/snapshots", response_model=list[SnapshotOut], tags=["games"])
async def list_snapshots(game: str, orch: Orch) -> list[SnapshotOut]:
    return [SnapshotOut.of(s) for s in await orch.snapshots(game)]


@router.post("/games/{game}/restore", response_model=BackupOut, tags=["games"])
async def pin_backup(game: str, orch: Orch, backup_id: Annotated[int, Query()]) -> BackupOut:
    backup = await orch.pin_backup(game, backup_id)
    await orch.audit(ACTOR, "restore", game, "ok", backup.filename)
    return BackupOut.of(backup)


@router.get("/host", response_model=HostOut | None, tags=["host"])
async def get_host(orch: Orch) -> HostOut | None:
    host = await orch.host()
    return HostOut.of(host, await orch.hourly_price()) if host else None


@router.post(
    "/host/shutdown", status_code=status.HTTP_202_ACCEPTED, response_model=Accepted, tags=["host"]
)
async def shutdown_host(orch: Orch, tasks: Runner) -> Accepted:
    op = orch.begin_host("shutting down")  # a duplicate request fails here with 409
    tasks.spawn("host shutdown", _audited(orch, "host-shutdown", None, orch.shutdown_host(op=op)))
    return Accepted(detail="stopping all games and deleting the host")


@router.post("/host/destroy", response_model=Accepted, tags=["host"])
async def destroy_host(orch: Orch, force: Annotated[bool, Query()] = False) -> Accepted:
    if not force:
        raise InvalidStateError("destroy deletes the VM even with unsaved data; pass force=true")
    deleted = await orch.destroy_host()
    await orch.audit(ACTOR, "host-destroy", None, "ok")
    return Accepted(accepted=deleted, detail="host deleted" if deleted else "no host")
