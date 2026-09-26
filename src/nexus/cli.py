"""``nexus``: a small admin client for the nexus HTTP API.

Reads NEXUS_URL (default http://localhost:8080) and NEXUS_API_TOKEN from the environment.
"""

import json
import os
from typing import Annotated, Any

import httpx
import typer

app = typer.Typer(help="Admin client for the nexus API.", no_args_is_help=True)
games = typer.Typer(help="Game servers.", no_args_is_help=True)
host = typer.Typer(help="The Hetzner host VM.", no_args_is_help=True)
app.add_typer(games, name="games")
app.add_typer(host, name="host")


def _client() -> httpx.Client:
    token = os.environ.get("NEXUS_API_TOKEN")
    if not token:
        raise typer.BadParameter("NEXUS_API_TOKEN is not set")
    return httpx.Client(
        base_url=os.environ.get("NEXUS_URL", "http://localhost:8080").rstrip("/") + "/api",
        headers={"X-API-KEY": token},
        timeout=30,
    )


def _call(method: str, path: str, **kwargs: Any) -> None:
    with _client() as client:
        response = client.request(method, path, **kwargs)
    try:
        body = response.json()
    except ValueError:
        body = response.text
    typer.echo(json.dumps(body, indent=2, default=str) if not isinstance(body, str) else body)
    if response.is_error:
        raise typer.Exit(1)


@games.command("list")
def games_list() -> None:
    _call("GET", "/games")


@games.command("status")
def games_status(game: str) -> None:
    _call("GET", f"/games/{game}")


@games.command("start")
def games_start(game: str) -> None:
    _call("POST", f"/games/{game}/start")


@games.command("stop")
def games_stop(game: str) -> None:
    _call("POST", f"/games/{game}/stop")


@games.command("backup")
def games_backup(game: str) -> None:
    _call("POST", f"/games/{game}/backup")


@games.command("backups")
def games_backups(game: str) -> None:
    _call("GET", f"/games/{game}/backups")


@games.command("snapshots")
def games_snapshots(game: str) -> None:
    _call("GET", f"/games/{game}/snapshots")


@games.command("restore")
def games_restore(game: str, backup_id: int) -> None:
    _call("POST", f"/games/{game}/restore", params={"backup_id": backup_id})


@host.command("status")
def host_status() -> None:
    _call("GET", "/host")


@host.command("shutdown")
def host_shutdown() -> None:
    _call("POST", "/host/shutdown")


@host.command("destroy")
def host_destroy(
    force: Annotated[
        bool, typer.Option("--force", help="Required: delete even unsaved data")
    ] = False,
) -> None:
    _call("POST", "/host/destroy", params={"force": force})
