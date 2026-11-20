"""Requirements 13-16: exact Europe/Madrid peak window with automatic transitions."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from ps5monitor.config import Settings
from ps5monitor.schedule import NORMAL, PEAK, fmt_local, interval_for, is_due, mode_at, next_transition

MAD = ZoneInfo("Europe/Madrid")
S = Settings()


def at(*a: int) -> datetime:
    return datetime(*a, tzinfo=MAD)


# 13
def test_peak_starts_exactly_monday_23_midnight() -> None:
    assert mode_at(at(2026, 11, 22, 23, 59, 59), S) == NORMAL
    assert mode_at(at(2026, 11, 23, 0, 0, 0), S) == PEAK


# 14
def test_peak_covers_all_of_monday_30_and_ends_dec_1() -> None:
    assert mode_at(at(2026, 11, 30, 23, 59, 59), S) == PEAK
    assert mode_at(at(2026, 12, 1, 0, 0, 0), S) == NORMAL
    assert next_transition(at(2026, 11, 30, 23, 59, 59), S) == (at(2026, 12, 1).astimezone(UTC), NORMAL)
    assert next_transition(at(2026, 12, 1), S) is None  # monitoring continues in normal mode


# 15
def test_every_minute_of_the_eight_days_is_peak_with_same_cadence() -> None:
    t = at(2026, 11, 23)
    end = at(2026, 12, 1)
    intervals = set()
    while t < end:
        assert mode_at(t, S) == PEAK, t
        intervals.add(interval_for("game", mode_at(t, S), S))
        t += timedelta(minutes=1)
    assert intervals == {timedelta(minutes=S.intervals["game"][1])}


@pytest.mark.parametrize("when", [at(2026, 11, 27, 3, 0), at(2026, 11, 28, 14, 0), at(2026, 11, 30, 23, 30)])
def test_nights_weekends_cyber_monday_are_peak(when: datetime) -> None:
    assert mode_at(when, S) == PEAK


# 16
def test_mode_from_any_timezone_representation_after_restart() -> None:
    assert S.peak_start_utc == datetime(2026, 11, 22, 23, 0, tzinfo=UTC)
    assert S.peak_end_exclusive_utc == datetime(2026, 11, 30, 23, 0, tzinfo=UTC)
    assert mode_at(datetime(2026, 11, 22, 22, 59, 59, tzinfo=UTC), S) == NORMAL
    assert mode_at(datetime(2026, 11, 22, 23, 0, 0, tzinfo=UTC), S) == PEAK
    assert fmt_local(datetime(2026, 11, 22, 23, 0, tzinfo=UTC), S) == "23/11/2026 00:00:00"
    with pytest.raises(ValueError):
        mode_at(datetime(2026, 11, 23), S)  # naive datetimes are refused


def test_due_logic_and_backoff() -> None:
    now = at(2026, 11, 25, 12, 0)
    assert is_due("game", None, None, now, S)
    assert is_due("game", now - timedelta(minutes=5), None, now, S)  # peak: 3-min interval
    assert not is_due("game", now - timedelta(minutes=1), None, now, S)
    assert not is_due("game", None, now + timedelta(minutes=10), now, S)
    normal = at(2026, 12, 5, 12, 0)
    assert not is_due("game", normal - timedelta(minutes=5), None, normal, S)  # normal: 15 min
    assert is_due("game", normal - timedelta(minutes=14), None, normal, S)  # tolerance for trigger jitter
