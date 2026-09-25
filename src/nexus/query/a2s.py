"""Steam A2S_INFO queries (Valheim and most Steam dedicated servers)."""

import logging

import a2s

log = logging.getLogger(__name__)


async def player_count(host: str, port: int, timeout: float = 3.0) -> int | None:
    try:
        info = await a2s.ainfo((host, port), timeout=timeout, encoding="utf-8")
    except (TimeoutError, OSError, a2s.BrokenMessageError, a2s.BufferExhaustedError) as exc:
        log.debug("a2s query %s:%s failed: %s", host, port, exc)
        return None
    return int(info.player_count) - int(getattr(info, "bot_count", 0) or 0)
