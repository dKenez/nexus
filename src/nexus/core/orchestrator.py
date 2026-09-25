"""The game/host state machine.

A single shared Hetzner VM ("the host") runs every active game as a container. The host only
exists while at least one game occupies it. Game data lives in ``BackupStore`` (ymir): it is
pushed to the host when a game starts and pulled back when it stops.

Safety invariant: the host is never deleted while a game on it is ``dirty`` (its data on the
host is newer than its newest backup), unless an admin forces it.

Concurrency: one lock per game serialises that game's operations; ``_host_lock`` serialises
anything that creates/deletes the host, changes the firewall or claims host capacity.
"""

import asyncio
import logging
import os
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from nexus.core.notify import Level, Notifier
from nexus.core.recipes import Recipe, RecipeBook
from nexus.db.models import AuditLog, Backup, GameState, GameStatus, Host, HostStatus, utcnow
from nexus.infra.agent import AgentFactory, HostAgent
from nexus.infra.backups import BackupStore
from nexus.infra.hetzner import HetznerGateway
from nexus.query.base import PlayerQuery, query_players

log = logging.getLogger(__name__)

Progress = Callable[[str], Awaitable[None]]


async def _no_progress(_: str) -> None:
    return None


class NexusError(Exception):
    """An error whose message is safe and useful to show to a Discord user."""


class InvalidStateError(NexusError):
    pass


class CapacityError(NexusError):
    pass


class HostUnsafeError(NexusError):
    pass


@dataclass(frozen=True)
class OrchestratorConfig:
    env: str
    server_type: str
    host_memory_reserve_mb: int
    backup_retention: int
    host_idle_grace: int
    provision_timeout: int = 600
    public_hostname: str | None = None


@dataclass(frozen=True)
class GameView:
    recipe: Recipe
    status: GameStatus
    since: datetime
    started_at: datetime | None
    players: int | None
    last_player_seen_at: datetime | None
    dirty: bool
    last_error: str | None
    pinned_backup_id: int | None


@dataclass(frozen=True)
class HostView:
    id: int
    name: str
    status: HostStatus
    hcloud_id: int | None
    server_type: str
    memory_mb: int
    created_at: datetime
    ready_at: datetime | None
    ip: str
    last_error: str | None


class Orchestrator:
    def __init__(
        self,
        *,
        config: OrchestratorConfig,
        recipes: RecipeBook,
        sessions: async_sessionmaker[AsyncSession],
        hetzner: HetznerGateway,
        agents: AgentFactory,
        backups: BackupStore,
        notifier: Notifier,
        user_data: Callable[[], str],
        ssh_public_key: str,
        query: PlayerQuery = query_players,
        clock: Callable[[], datetime] = utcnow,
        environ: Mapping[str, str] | None = None,
        poll_interval: float = 5.0,
    ) -> None:
        self.config = config
        self.recipes = recipes
        self._sessions = sessions
        self._hetzner = hetzner
        self._agents = agents
        self._backups = backups
        self.notifier = notifier
        self._user_data = user_data
        self._ssh_public_key = ssh_public_key
        self._query = query
        self._now = clock
        self._environ = os.environ if environ is None else environ
        self._poll_interval = poll_interval
        self._host_lock = asyncio.Lock()
        self._game_locks: dict[str, asyncio.Lock] = {}
        self._ip: str | None = None
        self._memory_mb: int | None = None

    # ------------------------------------------------------------------ helpers

    def _lock(self, game: str) -> asyncio.Lock:
        return self._game_locks.setdefault(game, asyncio.Lock())

    async def ip(self) -> str:
        if self._ip is None:
            self._ip = (await self._hetzner.primary_ip()).ip
        return self._ip

    async def address(self) -> str:
        return self.config.public_hostname or await self.ip()

    async def host_memory_mb(self) -> int:
        if self._memory_mb is None:
            self._memory_mb = await self._hetzner.server_memory_mb()
        return self._memory_mb

    def _recipe(self, game: str) -> Recipe:
        recipe = self.recipes.get(game)
        if not recipe.enabled:
            raise InvalidStateError(f"{recipe.display_name} is disabled")
        return recipe

    @staticmethod
    async def _get_state(s: AsyncSession, game: str) -> GameState:
        state = await s.get(GameState, game)
        if state is None:
            state = GameState(game=game, status=GameStatus.STOPPED, dirty=False)
            s.add(state)
            await s.flush()
        return state

    @staticmethod
    async def _active_host(s: AsyncSession) -> Host | None:
        result = await s.execute(
            select(Host).where(Host.status != HostStatus.DELETED).order_by(Host.id.desc()).limit(1)
        )
        return result.scalar_one_or_none()

    @staticmethod
    async def _occupying(s: AsyncSession) -> list[GameState]:
        result = await s.execute(select(GameState).where(GameState.status != GameStatus.STOPPED))
        return list(result.scalars())

    async def _set(self, game: str, status: GameStatus | None = None, **fields: object) -> None:
        async with self._sessions() as s, s.begin():
            state = await self._get_state(s, game)
            if status is not None and status is not state.status:
                state.status = status
                state.since = self._now()
            for key, value in fields.items():
                setattr(state, key, value)

    async def _state_of(self, game: str) -> GameState:
        async with self._sessions() as s, s.begin():
            return await self._get_state(s, game)

    # ------------------------------------------------------------------ views

    async def games(self) -> list[GameView]:
        async with self._sessions() as s:
            states = {st.game: st for st in (await s.execute(select(GameState))).scalars()}
        views = []
        for recipe in self.recipes.all():
            st = states.get(recipe.name)
            views.append(
                GameView(
                    recipe=recipe,
                    status=st.status if st else GameStatus.STOPPED,
                    since=st.since if st else self._now(),
                    started_at=st.started_at if st else None,
                    players=st.last_player_count if st else None,
                    last_player_seen_at=st.last_player_seen_at if st else None,
                    dirty=st.dirty if st else False,
                    last_error=st.last_error if st else None,
                    pinned_backup_id=st.pinned_backup_id if st else None,
                )
            )
        return views

    async def game(self, game: str) -> GameView:
        self.recipes.get(game)
        return next(v for v in await self.games() if v.recipe.name == game)

    async def host(self) -> HostView | None:
        async with self._sessions() as s:
            host = await self._active_host(s)
        if host is None:
            return None
        return HostView(
            id=host.id,
            name=host.name,
            status=host.status,
            hcloud_id=host.hcloud_id,
            server_type=host.server_type,
            memory_mb=host.memory_mb,
            created_at=host.created_at,
            ready_at=host.ready_at,
            ip=await self.ip(),
            last_error=host.last_error,
        )

    async def hourly_price(self) -> str | None:
        return await self._hetzner.hourly_price()

    async def backups(self, game: str) -> list[Backup]:
        self.recipes.get(game)
        async with self._sessions() as s:
            result = await s.execute(
                select(Backup)
                .where(Backup.game == game, Backup.deleted_at.is_(None))
                .order_by(Backup.created_at.desc())
            )
            return list(result.scalars())

    async def audit(
        self, actor: str, action: str, target: str | None, result: str, detail: str | None = None
    ) -> None:
        async with self._sessions() as s, s.begin():
            s.add(AuditLog(actor=actor, action=action, target=target, result=result, detail=detail))

    # ------------------------------------------------------------------ host

    async def _claim(self, recipe: Recipe) -> None:
        """Reserve host capacity for ``recipe`` and mark it STARTING. Caller holds the host lock."""
        capacity = await self.host_memory_mb() - self.config.host_memory_reserve_mb
        async with self._sessions() as s, s.begin():
            others = [st for st in await self._occupying(s) if st.game != recipe.name]
            used = sum(
                self.recipes.get(st.game).memory_mb for st in others if st.game in self.recipes
            )
            if used + recipe.memory_mb > capacity:
                running = ", ".join(st.game for st in others) or "nothing"
                raise CapacityError(
                    f"not enough memory on the host for {recipe.display_name} "
                    f"({used + recipe.memory_mb} MB needed, {capacity} MB available; "
                    f"running: {running})"
                )
            state = await self._get_state(s, recipe.name)
            state.status = GameStatus.STARTING
            state.since = self._now()
            state.last_error = None

    async def _sync_firewall(self) -> None:
        async with self._sessions() as s:
            occupying = await self._occupying(s)
        ports = [
            port
            for st in occupying
            if st.game in self.recipes
            for port in self.recipes.get(st.game).ports
        ]
        await self._hetzner.set_firewall(ports)

    async def _ensure_host(self, progress: Progress) -> Host:
        """Return a READY host, provisioning one if needed. Caller holds the host lock."""
        async with self._sessions() as s:
            host = await self._active_host(s)

        if host is not None and host.status is HostStatus.READY:
            if host.hcloud_id is not None and await self._hetzner.get_server(host.hcloud_id):
                return host
            await self._mark_host_gone(host.id, "server disappeared from Hetzner")
            host = None

        if host is not None:
            # A leftover PROVISIONING/FAILED/DELETING host from an earlier crash.
            await self._discard_host(host)

        memory = await self.host_memory_mb()
        now = self._now()
        async with self._sessions() as s, s.begin():
            host = Host(
                name=f"nexus-{self.config.env}-{now:%Y%m%d-%H%M%S}",
                status=HostStatus.PROVISIONING,
                server_type=self.config.server_type,
                memory_mb=memory,
                created_at=now,
            )
            s.add(host)
        await progress("provisioning a VM at Hetzner")
        try:
            await self._hetzner.ensure_ssh_key(self._ssh_public_key)
            await self._sync_firewall()
            server = await self._hetzner.create_server(host.name, self._user_data())
            async with self._sessions() as s, s.begin():
                row = await s.get_one(Host, host.id)
                row.hcloud_id = server.id
            await progress("waiting for the VM to boot")
            await self._agents.wait_ready(await self.ip(), self.config.provision_timeout)
        except Exception as exc:
            log.exception("provisioning %s failed", host.name)
            async with self._sessions() as s, s.begin():
                row = await s.get_one(Host, host.id)
                row.status = HostStatus.FAILED
                row.last_error = str(exc)
            async with self._sessions() as s:
                failed = await s.get_one(Host, host.id)
            await self._discard_host(failed)
            raise NexusError(f"provisioning the VM failed: {exc}") from exc

        async with self._sessions() as s, s.begin():
            row = await s.get_one(Host, host.id)
            row.status = HostStatus.READY
            row.ready_at = self._now()
            row.empty_since = None
        log.info("host %s ready", host.name)
        return row

    async def _discard_host(self, host: Host) -> None:
        """Delete a host that never became (or is no longer) usable. Refuses if data is dirty."""
        async with self._sessions() as s:
            dirty = [st.game for st in (await s.execute(select(GameState))).scalars() if st.dirty]
        if dirty:
            raise HostUnsafeError(
                f"host {host.name} is in state {host.status} but holds unsaved data for "
                f"{', '.join(dirty)}; an admin must resolve it"
            )
        if host.hcloud_id is not None and await self._hetzner.get_server(host.hcloud_id):
            await self._hetzner.delete_server(host.hcloud_id)
        async with self._sessions() as s, s.begin():
            row = await s.get_one(Host, host.id)
            row.status = HostStatus.DELETED
            row.deleted_at = self._now()

    async def _mark_host_gone(self, host_id: int, reason: str) -> None:
        lost: list[str] = []
        async with self._sessions() as s, s.begin():
            row = await s.get_one(Host, host_id)
            row.status = HostStatus.DELETED
            row.deleted_at = self._now()
            row.last_error = reason
            for st in await self._occupying(s):
                if st.dirty:
                    lost.append(st.game)
                st.status = GameStatus.STOPPED
                st.since = self._now()
                st.dirty = False
                st.last_error = reason
        if lost:
            await self.notifier.notify(
                Level.ERROR,
                f"Host vanished ({reason}). Progress since the last backup is lost for: "
                f"{', '.join(lost)}",
            )

    async def _teardown_if_empty(self, *, force: bool = False) -> bool:
        """Delete the host if nothing occupies it. Caller holds the host lock."""
        async with self._sessions() as s:
            host = await self._active_host(s)
            occupying = await self._occupying(s)
        if host is None:
            return False
        if occupying and not force:
            return False
        dirty = [st.game for st in occupying if st.dirty]
        if dirty and not force:
            raise HostUnsafeError(f"refusing to delete the host: unsaved data for {dirty}")

        async with self._sessions() as s, s.begin():
            row = await s.get_one(Host, host.id)
            row.status = HostStatus.DELETING
        if host.hcloud_id is not None and await self._hetzner.get_server(host.hcloud_id):
            await self._hetzner.delete_server(host.hcloud_id)
        async with self._sessions() as s, s.begin():
            row = await s.get_one(Host, host.id)
            row.status = HostStatus.DELETED
            row.deleted_at = self._now()
            for st in await self._occupying(s):
                st.status = GameStatus.STOPPED
                st.since = self._now()
                st.dirty = False
                st.last_error = "host destroyed by an admin"
        log.info("host %s deleted", host.name)
        if dirty:
            await self.notifier.notify(
                Level.WARNING, f"Host force-deleted with unsaved data for: {', '.join(dirty)}"
            )
        return True

    # ------------------------------------------------------------------ games

    async def start(self, game: str, progress: Progress = _no_progress) -> GameView:
        recipe = self._recipe(game)
        env = recipe.resolve_env(dict(self._environ))
        async with self._lock(game):
            state = await self._state_of(game)
            if state.status is GameStatus.RUNNING:
                raise InvalidStateError(f"{recipe.display_name} is already running")
            if state.status is not GameStatus.STOPPED and not (
                state.status is GameStatus.FAILED and not state.dirty
            ):
                raise InvalidStateError(f"{recipe.display_name} is {state.status}")

            async with self._host_lock:
                await self._claim(recipe)
                try:
                    host = await self._ensure_host(progress)
                    await self._sync_firewall()
                except BaseException as exc:
                    await self._set(game, GameStatus.STOPPED, host_id=None, last_error=str(exc))
                    await self._sync_firewall_quietly()
                    raise

            ip = await self.ip()
            try:
                await self._set(game, GameStatus.RESTORING, host_id=host.id)
                source = await self._restore_source(game)
                async with self._agents.connect(ip) as agent:
                    await progress(
                        f"restoring backup {source.filename}"
                        if source
                        else "no backup yet; new world"
                    )
                    await agent.restore(
                        game, self._backups.read(game, source.filename) if source else None
                    )
                    await progress("pulling the server image")
                    await agent.pull_image(recipe)
                    await self._set(game, GameStatus.STARTING, dirty=True, pinned_backup_id=None)
                    await agent.run_container(recipe, env)
            except BaseException as exc:
                await self._start_failed(game, exc)
                raise

            await progress("waiting for the server to answer")
            answering = await self._wait_answering(recipe, ip)
            now = self._now()
            await self._set(
                game,
                GameStatus.RUNNING,
                started_at=now,
                last_player_seen_at=now,
                last_player_count=0 if answering else None,
                last_error=None if answering else "server did not answer queries before timeout",
            )
            if not answering:
                await self.notifier.notify(
                    Level.WARNING,
                    f"{recipe.display_name} started but isn't answering queries yet.",
                )
        return await self.game(game)

    async def _start_failed(self, game: str, exc: BaseException) -> None:
        log.error("starting %s failed: %s", game, exc)
        state = await self._state_of(game)
        if state.dirty:
            # The container may have run and changed the world: keep the host for a stop/backup.
            await self._set(game, GameStatus.FAILED, last_error=f"start failed: {exc}")
            await self.notifier.notify(
                Level.ERROR, f"Starting {game} failed after it ran ({exc}). Run a stop to back up."
            )
            return
        await self._set(game, GameStatus.STOPPED, host_id=None, last_error=f"start failed: {exc}")
        async with self._host_lock:
            await self._sync_firewall_quietly()
            await self._teardown_quietly()

    async def _restore_source(self, game: str) -> Backup | None:
        async with self._sessions() as s:
            state = await s.get(GameState, game)
            if state and state.pinned_backup_id:
                pinned = await s.get(Backup, state.pinned_backup_id)
                if pinned and pinned.deleted_at is None:
                    return pinned
            result = await s.execute(
                select(Backup)
                .where(Backup.game == game, Backup.deleted_at.is_(None))
                .order_by(Backup.created_at.desc())
                .limit(1)
            )
            return result.scalar_one_or_none()

    async def _wait_answering(self, recipe: Recipe, ip: str) -> bool:
        if recipe.query.port is None:
            return True
        deadline = self._now() + timedelta(seconds=recipe.startup_timeout)
        while self._now() < deadline:
            if await self._query(recipe, ip) is not None:
                return True
            await asyncio.sleep(self._poll_interval)
        return False

    async def stop(
        self, game: str, reason: str = "manual", progress: Progress = _no_progress
    ) -> GameView:
        recipe = self.recipes.get(game)
        async with self._lock(game):
            await self._stop_locked(recipe, reason, progress)
        async with self._host_lock:
            await self._sync_firewall_quietly()
            await self._teardown_if_empty()
        return await self.game(game)

    async def _stop_locked(self, recipe: Recipe, reason: str, progress: Progress) -> None:
        game = recipe.name
        state = await self._state_of(game)
        if state.status is GameStatus.STOPPED:
            raise InvalidStateError(f"{recipe.display_name} is not running")
        async with self._sessions() as s:
            host = await self._active_host(s)
        if host is None or host.status is not HostStatus.READY:
            await self._set(
                game,
                GameStatus.STOPPED,
                host_id=None,
                dirty=False,
                started_at=None,
                last_player_count=None,
            )
            if state.dirty:
                await self.notifier.notify(
                    Level.ERROR, f"{game} had unsaved data but no host was found; it is lost."
                )
            return

        try:
            async with self._agents.connect(await self.ip()) as agent:
                await self._set(game, GameStatus.STOPPING)
                await progress("stopping the server")
                await agent.stop_container(recipe)
                if state.dirty:
                    await self._set(game, GameStatus.BACKING_UP)
                    await progress("backing up the world to ymir")
                    await self._take_backup(agent, game, reason)
                await agent.remove_container(recipe)
        except BaseException as exc:
            log.exception("stopping %s failed", game)
            await self._set(game, GameStatus.FAILED, last_error=f"stop failed: {exc}")
            await self.notifier.notify(
                Level.ERROR,
                f"Stopping {recipe.display_name} failed: {exc}. The VM is kept so no data is lost.",
            )
            raise NexusError(f"stopping {recipe.display_name} failed: {exc}") from exc

        await self._set(
            game,
            GameStatus.STOPPED,
            host_id=None,
            dirty=False,
            started_at=None,
            last_player_count=None,
            last_error=None,
        )

    async def _take_backup(self, agent: HostAgent, game: str, reason: str) -> Backup:
        written = await self._backups.write(
            game, reason, lambda sink: agent.archive(game, sink), now=self._now()
        )
        async with self._sessions() as s, s.begin():
            backup = Backup(
                game=game,
                filename=written.filename,
                bytes=written.bytes,
                sha256=written.sha256,
                reason=reason,
                created_at=self._now(),
            )
            s.add(backup)
        await self._prune(game)
        log.info("backed up %s to %s (%d bytes)", game, written.filename, written.bytes)
        return backup

    async def _prune(self, game: str) -> None:
        async with self._sessions() as s, s.begin():
            state = await s.get(GameState, game)
            pinned = state.pinned_backup_id if state else None
            result = await s.execute(
                select(Backup)
                .where(Backup.game == game, Backup.deleted_at.is_(None))
                .order_by(Backup.created_at.desc(), Backup.id.desc())
            )
            for backup in list(result.scalars())[self.config.backup_retention :]:
                if backup.id == pinned:
                    continue
                await self._backups.delete(game, backup.filename)
                backup.deleted_at = self._now()

    async def backup(self, game: str, progress: Progress = _no_progress) -> Backup:
        """Hot backup of a running game: stop the container, archive, start it again."""
        recipe = self._recipe(game)
        env = recipe.resolve_env(dict(self._environ))
        async with self._lock(game):
            state = await self._state_of(game)
            if state.status is not GameStatus.RUNNING:
                raise InvalidStateError(
                    f"{recipe.display_name} is {state.status}; stopped games are already backed up"
                )
            try:
                async with self._agents.connect(await self.ip()) as agent:
                    await self._set(game, GameStatus.STOPPING)
                    await progress("stopping the server for a consistent backup")
                    await agent.stop_container(recipe)
                    await self._set(game, GameStatus.BACKING_UP)
                    await progress("backing up the world to ymir")
                    backup = await self._take_backup(agent, game, "manual")
                    await self._set(game, GameStatus.STARTING, dirty=True)
                    await progress("starting the server again")
                    await agent.run_container(recipe, env)
            except BaseException as exc:
                await self._set(game, GameStatus.FAILED, last_error=f"backup failed: {exc}")
                await self.notifier.notify(Level.ERROR, f"Backup of {game} failed: {exc}")
                raise NexusError(f"backup of {recipe.display_name} failed: {exc}") from exc
            await self._set(game, GameStatus.RUNNING, last_player_seen_at=self._now())
        return backup

    async def pin_backup(self, game: str, backup_id: int) -> Backup:
        recipe = self.recipes.get(game)
        async with self._lock(game):
            state = await self._state_of(game)
            if state.status is not GameStatus.STOPPED:
                raise InvalidStateError(f"stop {recipe.display_name} before choosing a backup")
            async with self._sessions() as s, s.begin():
                backup = await s.get(Backup, backup_id)
                if backup is None or backup.game != game or backup.deleted_at is not None:
                    raise NexusError(f"no backup {backup_id} for {recipe.display_name}")
                if not await self._backups.exists(game, backup.filename):
                    raise NexusError(f"backup file {backup.filename} is missing on disk")
                row = await self._get_state(s, game)
                row.pinned_backup_id = backup.id
            return backup

    async def shutdown_host(self, progress: Progress = _no_progress) -> None:
        """Stop (and back up) every game, then delete the host."""
        async with self._sessions() as s:
            occupying = [st.game for st in await self._occupying(s)]
        for game in occupying:
            if game in self.recipes:
                await progress(f"stopping {game}")
                await self.stop(game, "shutdown", progress)
        async with self._host_lock:
            await self._teardown_if_empty()

    async def destroy_host(self) -> bool:
        """Delete the host even if games have unsaved data. Admin only."""
        async with self._host_lock:
            deleted = await self._teardown_if_empty(force=True)
            await self._sync_firewall_quietly()
            return deleted

    # ------------------------------------------------------------------ reconciliation

    async def poll_players(self) -> list[str]:
        """Query every running game; stop idle ones. Returns the games that were stopped."""
        stopped = []
        ip = await self.ip()
        async with self._sessions() as s:
            running = list(
                (
                    await s.execute(select(GameState).where(GameState.status == GameStatus.RUNNING))
                ).scalars()
            )
        for state in running:
            if state.game not in self.recipes:
                continue
            recipe = self.recipes.get(state.game)
            if recipe.query.port is None:
                continue
            count = await self._query(recipe, ip)
            now = self._now()
            seen = state.last_player_seen_at or state.started_at or now
            if count:
                seen = now
            await self._set(state.game, last_player_count=count, last_player_seen_at=seen)
            started = state.started_at or now
            in_grace = now < started + timedelta(minutes=recipe.startup_grace_minutes)
            if in_grace or now - seen < timedelta(seconds=recipe.idle_seconds):
                continue
            minutes = int((now - seen).total_seconds() // 60)
            log.info("%s idle for %d minutes; stopping", state.game, minutes)
            try:
                await self.stop(state.game, "idle")
            except (NexusError, InvalidStateError) as exc:
                log.warning("idle stop of %s failed: %s", state.game, exc)
                continue
            await self.notifier.notify(
                Level.INFO,
                f"{recipe.display_name} was empty for {minutes} minutes; stopped and backed up.",
            )
            stopped.append(state.game)
        return stopped

    async def gc_host(self) -> bool:
        """Delete a READY host that has been empty for longer than the grace period."""
        async with self._host_lock:
            async with self._sessions() as s, s.begin():
                host = await self._active_host(s)
                if host is None or host.status is not HostStatus.READY:
                    return False
                if await self._occupying(s):
                    host.empty_since = None
                    return False
                if host.empty_since is None:
                    host.empty_since = self._now()
                    return False
                empty_for = self._now() - host.empty_since
            if empty_for < timedelta(seconds=self.config.host_idle_grace):
                return False
            log.info("host empty for %s; deleting", empty_for)
            return await self._teardown_if_empty()

    async def reconcile_hosts(self) -> None:
        """Bring the DB's idea of the host in line with what exists at Hetzner."""
        adopted = False
        async with self._host_lock:
            servers = {srv.id: srv for srv in await self._hetzner.list_servers()}
            async with self._sessions() as s:
                host = await self._active_host(s)

            if host is not None and host.hcloud_id is None:
                # Provisioning never runs outside the host lock, so this row is left over from
                # a crash between "create server" and "save its id". Drop it; if the server
                # was created after all, it's adopted below.
                async with self._sessions() as s, s.begin():
                    row = await s.get_one(Host, host.id)
                    row.status = HostStatus.DELETED
                    row.deleted_at = self._now()
                    row.last_error = "interrupted while provisioning"
                host = None

            if host is not None and host.hcloud_id is not None and host.hcloud_id not in servers:
                if host.status in (HostStatus.READY, HostStatus.DELETING):
                    await self._mark_host_gone(host.id, "server no longer exists at Hetzner")
                else:
                    async with self._sessions() as s, s.begin():
                        row = await s.get_one(Host, host.id)
                        row.status = HostStatus.DELETED
                        row.deleted_at = self._now()
                host = None

            known = {host.hcloud_id} if host is not None else set()
            for server in servers.values():
                if server.id in known:
                    continue
                if host is None:
                    await self._adopt(server.id, server.name, server.server_type)
                    known.add(server.id)
                    adopted = True
                    await self.notifier.notify(
                        Level.WARNING, f"Adopted untracked nexus VM {server.name} ({server.id})."
                    )
                else:
                    await self.notifier.notify(
                        Level.WARNING,
                        f"Found an extra nexus VM {server.name} ({server.id}) that isn't the "
                        "current host. It was left alone; delete it by hand if it holds no data.",
                    )

        if adopted:
            await self.adopt_containers()

    async def adopt_containers(self) -> list[str]:
        """Claim game containers running on the host that the DB thinks are stopped.

        Their data may be newer than any backup, so they're marked RUNNING and dirty; the
        normal idle logic then stops and backs them up instead of the host GC deleting them.
        """
        async with self._sessions() as s:
            host = await self._active_host(s)
        if host is None or host.status is not HostStatus.READY:
            return []
        try:
            async with self._agents.connect(await self.ip()) as agent:
                containers = await agent.containers()
        except Exception as exc:
            log.warning("could not list containers on the host: %s", exc)
            return []
        adopted = []
        now = self._now()
        for game, container in containers.items():
            if game not in self.recipes:
                continue
            state = await self._state_of(game)
            if state.status is not GameStatus.STOPPED:
                continue
            await self._set(
                game,
                GameStatus.RUNNING if container.running else GameStatus.FAILED,
                host_id=host.id,
                dirty=True,
                started_at=now,
                last_player_seen_at=now,
                last_error=None if container.running else "found stopped on an adopted host",
            )
            adopted.append(game)
        if adopted:
            await self.notifier.notify(
                Level.WARNING, f"Adopted untracked game containers: {', '.join(adopted)}"
            )
        return adopted

    async def _adopt(self, hcloud_id: int, name: str, server_type: str) -> None:
        async with self._sessions() as s, s.begin():
            s.add(
                Host(
                    hcloud_id=hcloud_id,
                    name=name,
                    status=HostStatus.READY,
                    server_type=server_type,
                    memory_mb=await self.host_memory_mb(),
                    created_at=self._now(),
                    ready_at=self._now(),
                )
            )

    async def recover(self) -> None:
        """After a restart: settle games left in intermediate states."""
        await self.reconcile_hosts()
        await self.adopt_containers()
        async with self._sessions() as s:
            host = await self._active_host(s)
            states = list((await s.execute(select(GameState))).scalars())
        containers = {}
        if host is not None and host.status is HostStatus.READY:
            try:
                async with self._agents.connect(await self.ip()) as agent:
                    containers = await agent.containers()
            except Exception as exc:
                log.warning("could not list containers on the host: %s", exc)
                return

        for state in states:
            if state.status in (GameStatus.STOPPED, GameStatus.FAILED):
                continue
            if state.game not in self.recipes:
                continue
            container = containers.get(state.game)
            running = container is not None and container.running
            starting = (GameStatus.STARTING, GameStatus.RESTORING, GameStatus.RUNNING)
            if state.status in starting and running:
                if state.status is not GameStatus.RUNNING:
                    now = self._now()
                    await self._set(
                        state.game, GameStatus.RUNNING, started_at=now, last_player_seen_at=now
                    )
                continue
            log.info("recovering %s from %s", state.game, state.status)
            try:
                await self.stop(state.game, "recovery")
            except Exception as exc:
                log.warning("recovery of %s failed: %s", state.game, exc)

    async def _sync_firewall_quietly(self) -> None:
        try:
            await self._sync_firewall()
        except Exception:
            log.exception("updating the firewall failed")

    async def _teardown_quietly(self) -> None:
        try:
            await self._teardown_if_empty()
        except Exception:
            log.exception("deleting the empty host failed")
