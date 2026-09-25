"""The background loop: player polling, idle stops, host GC and drift repair."""

import asyncio
import logging

from nexus.core.orchestrator import Orchestrator

log = logging.getLogger(__name__)


class Reconciler:
    def __init__(self, orchestrator: Orchestrator, interval: float) -> None:
        self._orchestrator = orchestrator
        self._interval = interval
        self.healthy = False

    async def tick(self) -> None:
        await self._orchestrator.reconcile_hosts()
        await self._orchestrator.poll_players()
        await self._orchestrator.gc_host()

    async def run(self) -> None:
        try:
            await self._orchestrator.recover()
        except Exception:
            log.exception("startup recovery failed")
        while True:
            try:
                await self.tick()
                self.healthy = True
            except asyncio.CancelledError:
                raise
            except Exception:
                self.healthy = False
                log.exception("reconcile tick failed")
            await asyncio.sleep(self._interval)
