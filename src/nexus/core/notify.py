"""Out-of-band notifications (idle stops, failures). The Discord bot plugs in here."""

import logging
from enum import StrEnum
from typing import Protocol

log = logging.getLogger(__name__)


class Level(StrEnum):
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"


class Notifier(Protocol):
    async def notify(self, level: Level, message: str) -> None: ...


class LogNotifier:
    async def notify(self, level: Level, message: str) -> None:
        log.log(
            {Level.INFO: logging.INFO, Level.WARNING: logging.WARNING}.get(level, logging.ERROR),
            "notify: %s",
            message,
        )


class FanoutNotifier:
    """Sends to every registered notifier; failures of one never affect the others."""

    def __init__(self, *notifiers: Notifier) -> None:
        self._notifiers = list(notifiers)

    def add(self, notifier: Notifier) -> None:
        self._notifiers.append(notifier)

    async def notify(self, level: Level, message: str) -> None:
        for notifier in self._notifiers:
            try:
                await notifier.notify(level, message)
            except Exception:
                log.exception("notifier %r failed", notifier)
