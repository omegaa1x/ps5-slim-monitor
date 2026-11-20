"""Timezone-aware peak/normal mode and per-source due computation."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from ps5monitor.config import Settings

PEAK = "peak"
NORMAL = "normal"
# Triggers are not perfectly punctual; treat a source as due slightly early so a 5-min trigger
# with a 5-min interval does not skip every other run.
DUE_TOLERANCE = timedelta(seconds=75)


def utcnow() -> datetime:
    return datetime.now(UTC)


def mode_at(now: datetime, settings: Settings) -> str:
    """Peak mode on [start, end_exclusive) in the configured local time zone."""
    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    return PEAK if settings.peak_start_utc <= now < settings.peak_end_exclusive_utc else NORMAL


def next_transition(now: datetime, settings: Settings) -> tuple[datetime, str] | None:
    if now < settings.peak_start_utc:
        return settings.peak_start_utc, PEAK
    if now < settings.peak_end_exclusive_utc:
        return settings.peak_end_exclusive_utc, NORMAL
    return None


def interval_for(source: str, mode: str, settings: Settings) -> timedelta:
    normal, peak = settings.intervals[source]
    return timedelta(minutes=peak if mode == PEAK else normal)


def is_due(source: str, last_attempt: datetime | None, backoff_until: datetime | None,
           now: datetime, settings: Settings) -> bool:
    if backoff_until is not None and now < backoff_until:
        return False
    if last_attempt is None:
        return True
    return now - last_attempt + DUE_TOLERANCE >= interval_for(source, mode_at(now, settings), settings)


def local(dt: datetime, settings: Settings) -> datetime:
    return dt.astimezone(settings.tz)


def fmt_local(dt: datetime, settings: Settings) -> str:
    return local(dt, settings).strftime("%d/%m/%Y %H:%M:%S")
