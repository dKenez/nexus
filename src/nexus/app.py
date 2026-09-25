"""Process wiring: settings → services → FastAPI app with the bot and reconciler as tasks."""

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress

from fastapi import FastAPI

from nexus.api.app import build_api
from nexus.config import Environment, Settings, get_settings
from nexus.core.notify import FanoutNotifier, LogNotifier
from nexus.core.orchestrator import Orchestrator, OrchestratorConfig
from nexus.core.recipes import RecipeBook
from nexus.core.reconciler import Reconciler
from nexus.core.tasks import TaskRunner
from nexus.db.session import make_engine, make_sessionmaker, upgrade
from nexus.infra.agent import SshAgentFactory
from nexus.infra.backups import BackupStore
from nexus.infra.cloudinit import HostKeys, render_user_data
from nexus.infra.hetzner import HcloudGateway

log = logging.getLogger(__name__)


def build_gateway(settings: Settings) -> HcloudGateway:
    return HcloudGateway(
        token=settings.hcloud_token.get_secret_value(),
        env=settings.nexus_env.value,
        primary_ip=settings.hcloud_primary_ip,
        server_type=settings.hcloud_server_type,
        image=settings.hcloud_image,
        ssh_key_name=settings.hcloud_ssh_key_name,
        firewall_name=settings.hcloud_firewall_name,
        ssh_cidrs=settings.ssh_allowed_cidrs,
        forbidden_primary_ip=(
            settings.hcloud_forbidden_primary_ip if settings.nexus_env is Environment.DEV else None
        ),
    )


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings: Settings = app.state.settings
    log.info("nexus starting (env=%s)", settings.nexus_env)

    recipes = RecipeBook.load(settings.recipes_dir)
    log.info("recipes: %s", ", ".join(r.name for r in recipes.all()) or "none")

    await upgrade(settings.database_url)
    engine = make_engine(settings.database_url)
    sessions = make_sessionmaker(engine)

    hetzner = build_gateway(settings)
    await hetzner.check_environment()

    backups = BackupStore(settings.backup_dir)
    await backups.check_writable()

    keys = HostKeys(
        client_private=settings.ssh_client_key.get_secret_value(),
        host_private=settings.ssh_host_key.get_secret_value(),
    )
    notifier = FanoutNotifier(LogNotifier())
    orchestrator = Orchestrator(
        config=OrchestratorConfig(
            env=settings.nexus_env.value,
            server_type=settings.hcloud_server_type,
            host_memory_reserve_mb=settings.host_memory_reserve_mb,
            backup_retention=settings.backup_retention,
            host_idle_grace=settings.host_idle_grace,
            public_hostname=settings.public_hostname,
        ),
        recipes=recipes,
        sessions=sessions,
        hetzner=hetzner,
        agents=SshAgentFactory(
            keys=keys, user=settings.ssh_user, connect_timeout=settings.ssh_connect_timeout
        ),
        backups=backups,
        notifier=notifier,
        user_data=lambda: render_user_data(
            keys=keys,
            ssh_user=settings.ssh_user,
            registry_user=settings.ghcr_pull_user,
            registry_token=(
                settings.ghcr_pull_token.get_secret_value() if settings.ghcr_pull_token else None
            ),
        ),
        ssh_public_key=keys.client_public,
    )
    tasks = TaskRunner(notifier)
    reconciler = Reconciler(orchestrator, settings.reconcile_interval)

    app.state.sessions = sessions
    app.state.orchestrator = orchestrator
    app.state.tasks = tasks
    app.state.reconciler = reconciler

    background = [asyncio.create_task(reconciler.run(), name="reconciler")]
    bot = None
    if settings.discord_token is not None:
        from nexus.bot.client import NexusBot
        from nexus.bot.permissions import RoleTiers

        bot = NexusBot(
            orchestrator=orchestrator,
            role_tiers=RoleTiers.of(
                settings.discord_roles_viewer,
                settings.discord_roles_operator,
                settings.discord_roles_admin,
            ),
            guild_id=settings.discord_guild_id,
            notify_channel_id=settings.discord_notify_channel_id,
        )
        notifier.add(bot)
        background.append(
            asyncio.create_task(bot.start(settings.discord_token.get_secret_value()), name="bot")
        )
    else:
        log.warning("DISCORD_TOKEN not set; the Discord bot is disabled")

    try:
        yield
    finally:
        log.info("nexus shutting down")
        await tasks.shutdown()
        if bot is not None:
            await bot.close()
        for task in background:
            task.cancel()
        for task in background:
            with suppress(asyncio.CancelledError, Exception):
                await task
        await engine.dispose()


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    app = build_api(lifespan=lifespan)
    app.state.settings = settings
    app.state.api_token = settings.nexus_api_token.get_secret_value()
    return app
