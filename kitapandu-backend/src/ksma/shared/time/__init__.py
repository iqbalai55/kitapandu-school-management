"""Injectable clock + timezone helpers.

Per Q11 / Q-default-timezone proposal in the docs, pilot uses ``Asia/Jakarta``,
but every ``Instant`` column is stored as ``TIMESTAMPTZ`` (UTC) with the tenant's
``default_timezone`` for display. Date-only columns are calendar dates (no TZ).
"""
from __future__ import annotations

from datetime import date, datetime, time, timezone, tzinfo
from typing import Protocol
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from ksma.settings import get_settings


class Clock(Protocol):
    def now(self) -> datetime: ...
    def today(self) -> date: ...


class SystemClock:
    """Production clock. UTC always."""

    def now(self) -> datetime:
        return datetime.now(timezone.utc)

    def today(self) -> date:
        return datetime.now(timezone.utc).date()


class FrozenClock:
    """Test double — wrap ``datetime.now(tz=UTC)`` to a fixed value."""

    def __init__(self, fixed: datetime) -> None:
        if fixed.tzinfo is None:
            fixed = fixed.replace(tzinfo=timezone.utc)
        self._fixed = fixed

    def now(self) -> datetime:
        return self._fixed

    def today(self) -> date:
        return self._fixed.date()


_default_clock: Clock = SystemClock()


def clock() -> Clock:
    """Return the current process clock (override in tests)."""
    return _default_clock


def set_clock(c: Clock) -> None:
    """Swap the process clock (used by tests via fixture)."""
    global _default_clock
    _default_clock = c


def reset_clock() -> None:
    global _default_clock
    _default_clock = SystemClock()


def default_timezone() -> tzinfo:
    """IANA tz from settings (per docs: Asia/Jakarta)."""
    try:
        return ZoneInfo(get_settings().default_timezone)
    except ZoneInfoNotFoundError as exc:
        raise RuntimeError(
            f"Invalid KITAPANDU_DEFAULT_TIMEZONE: {exc}"
        ) from exc


def local_date(d: date | datetime, *, tz: tzinfo | None = None) -> date:
    """Return the calendar date as seen in ``tz`` for ``d``.

    Date columns are stored as ``Date`` (no TZ). Datetime columns need conversion.
    """
    if isinstance(d, datetime):
        if d.tzinfo is None:
            d = d.replace(tzinfo=timezone.utc)
        return d.astimezone(tz or default_timezone()).date()
    return d


def combine_to_instant(d: date, t: time, *, tz: tzinfo | None = None) -> datetime:
    """Combine a local calendar date + local time → aware ``datetime`` in ``tz``."""
    zone = tz or default_timezone()
    return datetime.combine(d, t, tzinfo=zone)