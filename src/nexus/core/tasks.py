"""Fire-and-forget operations (API-triggered starts/stops) with error reporting."""

import asyncio
import logging
from collections.abc import Coroutine
from typing import Any

from nexus.core.notify import Level, Notifier

log = logging.getLogger(__name__)


class TaskRunner:
    def __init__(self, notifier: Notifier) -> None:
        self._notifier = notifier
        self._tasks: set[asyncio.Task[Any]] = set()

    def spawn(self, name: str, coro: Coroutine[Any, Any, Any]) -> None:
        task = asyncio.create_task(self._guard(name, coro), name=name)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _guard(self, name: str, coro: Coroutine[Any, Any, Any]) -> None:
        try:
            await coro
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.exception("background task %s failed", name)
            await self._notifier.notify(Level.ERROR, f"{name} failed: {exc}")

    async def shutdown(self) -> None:
        for task in list(self._tasks):
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
