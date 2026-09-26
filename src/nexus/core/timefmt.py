"""How times and durations are shown to people (Discord, notifications)."""

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo


def clock(moment: datetime, tz: ZoneInfo) -> str:
    """``05:15 CEST``"""
    return moment.astimezone(tz).strftime("%H:%M %Z")


def date_clock(moment: datetime, tz: ZoneInfo) -> str:
    """``2026-09-26 05:15 CEST``"""
    return moment.astimezone(tz).strftime("%Y-%m-%d %H:%M %Z")


def duration(span: timedelta | float) -> str:
    """``45s``, ``2m 13s``, ``1h 05m``"""
    seconds = int(span.total_seconds() if isinstance(span, timedelta) else span)
    if seconds < 60:
        return f"{seconds}s"
    minutes, secs = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes}m {secs:02d}s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h {minutes:02d}m"
