"""In-memory stand-ins for Hetzner and the SSH host agent. Tests never reach real services."""

from collections.abc import AsyncIterator, Iterable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from nexus.core.notify import Event, Level
from nexus.core.recipes import Port, Recipe
from nexus.infra.agent import ContainerInfo, RemoteFile, RemoteListing, Sink
from nexus.infra.hetzner import PrimaryIPInfo, ServerInfo

IP = "203.0.113.10"


class FakeClock:
    def __init__(self) -> None:
        self.now = datetime(2026, 9, 26, 12, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **kwargs: float) -> None:
        self.now += timedelta(**kwargs)


@dataclass
class FakeHetzner:
    memory_mb: int = 8192
    servers: dict[int, ServerInfo] = field(default_factory=dict)
    firewall: list[Port] = field(default_factory=list)
    created: int = 0
    deleted: list[int] = field(default_factory=list)
    fail_create: bool = False
    ssh_keys: list[str] = field(default_factory=list)
    _next_id: int = 100

    async def check_environment(self) -> None:
        return None

    async def primary_ip(self) -> PrimaryIPInfo:
        assignee = next(iter(self.servers), None)
        return PrimaryIPInfo(id=1, ip=IP, location="fsn1", assignee_id=assignee)

    async def list_servers(self) -> list[ServerInfo]:
        return list(self.servers.values())

    async def get_server(self, server_id: int) -> ServerInfo | None:
        return self.servers.get(server_id)

    async def create_server(self, name: str, user_data: str) -> ServerInfo:
        if self.fail_create:
            raise RuntimeError("hetzner is down")
        assert not self.servers, "the primary IP can only be on one server"
        self._next_id += 1
        self.created += 1
        server = ServerInfo(
            id=self._next_id,
            name=name,
            status="running",
            ip=IP,
            server_type="cx32",
            labels={"managed-by": "nexus", "nexus-env": "test"},
        )
        self.servers[server.id] = server
        return server

    async def delete_server(self, server_id: int) -> None:
        self.servers.pop(server_id)
        self.deleted.append(server_id)

    async def server_memory_mb(self) -> int:
        return self.memory_mb

    async def hourly_price(self) -> str | None:
        return "0.0120"

    async def ensure_ssh_key(self, public_key: str) -> None:
        self.ssh_keys.append(public_key)

    async def set_firewall(self, ports: Iterable[Port]) -> None:
        self.firewall = sorted(set(ports), key=lambda p: (p.port, p.protocol))


@dataclass
class FakeAgent:
    """One game host: per-game data blobs and containers."""

    data: dict[str, bytes] = field(default_factory=dict)
    containers_: dict[str, ContainerInfo] = field(default_factory=dict)
    env: dict[str, dict[str, str]] = field(default_factory=dict)
    fail_archive: bool = False
    pulled: list[str] = field(default_factory=list)
    # Files the game image wrote inside its data dir: game -> relative path -> (content, mtime)
    files: dict[str, dict[str, tuple[bytes, float]]] = field(default_factory=dict)
    remote_now: float = 0.0
    fail_read: bool = False
    # Container log lines per game (for query.type = "log").
    logs: dict[str, list[str]] = field(default_factory=dict)

    async def containers(self) -> dict[str, ContainerInfo]:
        return dict(self.containers_)

    async def pull_image(self, recipe: Recipe) -> None:
        self.pulled.append(recipe.image_ref)

    async def restore(self, game: str, chunks: AsyncIterator[bytes] | None) -> None:
        blob = b""
        if chunks is not None:
            async for chunk in chunks:
                blob += chunk
        self.data[game] = blob

    async def run_container(self, recipe: Recipe, env: dict[str, str]) -> None:
        self.env[recipe.name] = env
        self.containers_[recipe.name] = ContainerInfo(
            game=recipe.name, name=recipe.container_name, running=True, status="Up"
        )
        # The "game" writes to its world.
        self.data[recipe.name] = self.data.get(recipe.name, b"") + b"+played"

    async def stop_container(self, recipe: Recipe) -> None:
        if recipe.name in self.containers_:
            self.containers_[recipe.name] = ContainerInfo(
                game=recipe.name, name=recipe.container_name, running=False, status="Exited"
            )

    async def remove_container(self, recipe: Recipe) -> None:
        self.containers_.pop(recipe.name, None)

    async def archive(self, game: str, sink: Sink, exclude: tuple[str, ...] = ()) -> None:
        if self.fail_archive:
            raise RuntimeError("disk on fire")
        await sink(self.data[game])

    async def list_files(self, game: str, subdir: str) -> RemoteListing:
        prefix = subdir + "/"
        files = [
            RemoteFile(name=path.removeprefix(prefix), size=len(content), mtime=mtime)
            for path, (content, mtime) in self.files.get(game, {}).items()
            if path.startswith(prefix) and "/" not in path.removeprefix(prefix)
        ]
        return RemoteListing(now=self.remote_now, files=files)

    async def log_lines(self, recipe: Recipe, markers: list[str]) -> list[str]:
        return [line for line in self.logs.get(recipe.name, []) if any(m in line for m in markers)]

    async def read_file(self, game: str, path: str, sink: Sink) -> None:
        if self.fail_read:
            raise RuntimeError("connection reset")
        await sink(self.files[game][path][0])


@dataclass
class FakeAgentFactory:
    agent: FakeAgent = field(default_factory=FakeAgent)
    ready_calls: int = 0
    fail_ready: bool = False

    @asynccontextmanager
    async def connect(self, ip: str) -> AsyncIterator[FakeAgent]:
        assert ip == IP
        yield self.agent

    async def wait_ready(self, ip: str, timeout: float) -> None:
        self.ready_calls += 1
        if self.fail_ready:
            raise TimeoutError("never booted")
        # A fresh VM has an empty disk.
        self.agent.data.clear()
        self.agent.containers_.clear()


@dataclass
class RecordingNotifier:
    events: list[Event] = field(default_factory=list)

    async def notify(self, event: Event) -> None:
        self.events.append(event)

    @property
    def messages(self) -> list[tuple[Level, str]]:
        return [(e.level, e.text()) for e in self.events]


class FakeQuery:
    def __init__(self) -> None:
        self.players: dict[str, int | None] = {}

    async def __call__(self, recipe: Recipe, host: str) -> int | None:
        return self.players.get(recipe.name, 0)
