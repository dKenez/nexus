"""Out-of-band notifications (starts, stops, VM deletions, failures).

Everything is an ``Event`` with the same shape as a line of ``/games``: a title (the game, or
"Host"), a status icon and summary, and details joined with `·`. The Discord bot renders it as
an embed; the log gets its ``text()``.
"""

import logging
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol

log = logging.getLogger(__name__)


class Level(StrEnum):
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"


class Tone(StrEnum):
    """The colour of an event: what it means for the people playing."""

    GOOD = "good"  # a server is up
    NEUTRAL = "neutral"  # stopped, deleted, informational
    WARNING = "warning"
    ERROR = "error"


@dataclass(frozen=True)
class Event:
    level: Level
    title: str
    summary: str
    icon: str = "\N{INFORMATION SOURCE}\ufe0f"
    details: tuple[str, ...] = ()
    footer: str | None = None
    tone: Tone | None = None

    @property
    def colour(self) -> Tone:
        if self.tone is not None:
            return self.tone
        return {Level.WARNING: Tone.WARNING, Level.ERROR: Tone.ERROR}.get(self.level, Tone.NEUTRAL)

    def line(self) -> str:
        """``🟢 **up** · `1.2.3.4:2456` · started in 2m 13s``, like a line of /games."""
        return " · ".join((f"{self.icon} **{self.summary}**", *self.details))

    def text(self) -> str:
        """Plain text, for logs and tests."""
        text = " · ".join((f"{self.title}: {self.summary}", *self.details))
        return f"{text} ({self.footer})" if self.footer else text


class Notifier(Protocol):
    async def notify(self, event: Event) -> None: ...


class LogNotifier:
    async def notify(self, event: Event) -> None:
        level = {Level.INFO: logging.INFO, Level.WARNING: logging.WARNING}.get(
            event.level, logging.ERROR
        )
        log.log(level, "notify: %s", event.text())


class FanoutNotifier:
    """Sends to every registered notifier; failures of one never affect the others."""

    def __init__(self, *notifiers: Notifier) -> None:
        self._notifiers = list(notifiers)

    def add(self, notifier: Notifier) -> None:
        self._notifiers.append(notifier)

    async def notify(self, event: Event) -> None:
        for notifier in self._notifiers:
            try:
                await notifier.notify(event)
            except Exception:
                log.exception("notifier %r failed", notifier)
