"""Operating a game host over SSH: containers, data restore and data archive.

``HostAgent`` is the interface the orchestrator uses; ``SshHostAgent`` implements it with asyncssh.
Compression runs on the host (``zstd``), so archives stream through nexus untouched.
"""

import asyncio
import json
import logging
import shlex
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass
from typing import Protocol

import asyncssh

from nexus.core.recipes import Recipe
from nexus.infra.cloudinit import ENV_DIR, GAMES_DIR, READY_MARKER, HostKeys

log = logging.getLogger(__name__)

CHUNK = 1 << 20
GAME_LABEL = "nexus.game"

Sink = Callable[[bytes], Awaitable[None]]


class AgentError(Exception):
    pass


@dataclass(frozen=True)
class ContainerInfo:
    game: str
    name: str
    running: bool
    status: str


def data_dir(game: str) -> str:
    return f"{GAMES_DIR}/{game}/data"


def env_file(game: str) -> str:
    # Outside the data dir (never archived) and owned by the ssh user, because the docker
    # CLI reads --env-file client-side.
    return f"{ENV_DIR}/{game}.env"


class HostAgent(Protocol):
    async def containers(self) -> dict[str, ContainerInfo]: ...
    async def pull_image(self, recipe: Recipe) -> None: ...
    async def restore(self, game: str, chunks: AsyncIterator[bytes] | None) -> None: ...
    async def run_container(self, recipe: Recipe, env: dict[str, str]) -> None: ...
    async def stop_container(self, recipe: Recipe) -> None: ...
    async def remove_container(self, recipe: Recipe) -> None: ...
    async def archive(self, game: str, sink: Sink) -> None: ...


class AgentFactory(Protocol):
    def connect(self, ip: str) -> AbstractAsyncContextManager[HostAgent]: ...
    async def wait_ready(self, ip: str, timeout: float) -> None: ...


def docker_run_command(recipe: Recipe) -> str:
    args = [
        "docker",
        "run",
        "--detach",
        "--name",
        recipe.container_name,
        "--label",
        f"{GAME_LABEL}={recipe.name}",
        "--restart",
        "unless-stopped",
        "--stop-timeout",
        str(recipe.stop_grace_seconds),
        "--env-file",
        env_file(recipe.name),
        "--volume",
        f"{data_dir(recipe.name)}:{recipe.data_path}",
    ]
    for port in recipe.ports:
        args += ["--publish", f"{port.port}:{port.port}/{port.protocol}"]
    args.append(recipe.image_ref)
    return shlex.join(args)


def render_env_file(env: dict[str, str]) -> str:
    lines = []
    for key, value in sorted(env.items()):
        if "\n" in value or "\n" in key:
            raise AgentError(f"env var {key!r} contains a newline; docker env files can't hold it")
        lines.append(f"{key}={value}")
    return "\n".join(lines) + "\n"


def parse_ps(output: str) -> dict[str, ContainerInfo]:
    containers: dict[str, ContainerInfo] = {}
    for line in output.splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        labels = dict(
            item.split("=", 1) for item in row.get("Labels", "").split(",") if "=" in item
        )
        game = labels.get(GAME_LABEL)
        if not game:
            continue
        containers[game] = ContainerInfo(
            game=game,
            name=row["Names"],
            running=row.get("State") == "running",
            status=row.get("Status", ""),
        )
    return containers


class SshHostAgent:
    def __init__(self, conn: asyncssh.SSHClientConnection) -> None:
        self._conn = conn

    async def _run(self, command: str, *, input: str | None = None, check: bool = True) -> str:
        result = await self._conn.run(command, input=input, check=False)
        if check and result.exit_status != 0:
            raise AgentError(
                f"`{command}` failed ({result.exit_status}): {result.stderr!s}".strip()
            )
        return str(result.stdout or "")

    async def containers(self) -> dict[str, ContainerInfo]:
        out = await self._run(
            f"docker ps --all --filter label={GAME_LABEL} --format '{{{{json .}}}}'"
        )
        return parse_ps(out)

    async def pull_image(self, recipe: Recipe) -> None:
        await self._run(shlex.join(["docker", "pull", "--quiet", recipe.image_ref]))

    async def restore(self, game: str, chunks: AsyncIterator[bytes] | None) -> None:
        target = shlex.quote(data_dir(game))
        await self._run(f"sudo rm -rf {target} && sudo mkdir -p {target}")
        if chunks is None:
            return
        command = f"bash -o pipefail -c {shlex.quote(f'zstd -dc | sudo tar -C {target} -xf -')}"
        async with self._conn.create_process(command, encoding=None) as proc:
            async for chunk in chunks:
                proc.stdin.write(chunk)
                await proc.stdin.drain()
            proc.stdin.write_eof()
            result = await proc.wait()
        if result.exit_status != 0:
            raise AgentError(f"restore of {game} failed: {result.stderr!r}")

    async def run_container(self, recipe: Recipe, env: dict[str, str]) -> None:
        path = shlex.quote(env_file(recipe.name))
        await self._run(f"umask 077 && cat > {path}", input=render_env_file(env))
        await self._run(shlex.join(["docker", "rm", "--force", recipe.container_name]), check=False)
        await self._run(docker_run_command(recipe))

    async def stop_container(self, recipe: Recipe) -> None:
        await self._run(
            shlex.join(
                ["docker", "stop", "--time", str(recipe.stop_grace_seconds), recipe.container_name]
            ),
            check=False,
        )

    async def remove_container(self, recipe: Recipe) -> None:
        # Missing containers are fine: the goal is that none exists afterwards.
        await self._run(shlex.join(["docker", "rm", "--force", recipe.container_name]), check=False)

    async def archive(self, game: str, sink: Sink) -> None:
        source = shlex.quote(data_dir(game))
        pipeline = f"sudo tar -C {source} -cf - . | zstd -3 -T0 -c"
        command = f"bash -o pipefail -c {shlex.quote(pipeline)}"
        async with self._conn.create_process(command, encoding=None) as proc:
            while chunk := await proc.stdout.read(CHUNK):
                await sink(chunk)
            result = await proc.wait()
        if result.exit_status != 0:
            raise AgentError(f"archive of {game} failed: {result.stderr!r}")


class SshAgentFactory:
    def __init__(self, *, keys: HostKeys, user: str, connect_timeout: int = 10) -> None:
        self._client_key = asyncssh.import_private_key(keys.client_private)
        self._host_public = keys.host_public
        self._user = user
        self._timeout = connect_timeout

    def _connect(self, ip: str):
        return asyncssh.connect(
            ip,
            username=self._user,
            client_keys=[self._client_key],
            known_hosts=asyncssh.import_known_hosts(f"{ip} {self._host_public}\n"),
            connect_timeout=self._timeout,
            keepalive_interval=30,
        )

    @asynccontextmanager
    async def connect(self, ip: str) -> AsyncIterator[HostAgent]:
        async with self._connect(ip) as conn:
            yield SshHostAgent(conn)

    async def wait_ready(self, ip: str, timeout: float) -> None:
        """Wait until SSH answers and cloud-init has written the ready marker."""
        deadline = time.monotonic() + timeout
        last_error: Exception | None = None
        while time.monotonic() < deadline:
            try:
                async with self._connect(ip) as conn:
                    result = await conn.run(f"test -f {READY_MARKER}", check=False)
                    if result.exit_status == 0:
                        return
                    last_error = AgentError("cloud-init has not finished")
            except (OSError, asyncssh.Error, TimeoutError) as exc:
                last_error = exc
            await asyncio.sleep(5)
        raise AgentError(f"host {ip} not ready after {timeout:.0f}s: {last_error}")
