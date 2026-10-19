"""Unit tests for the Clock protocol + Asia/Jakarta timezone helpers.

Seam: the Clock protocol (now() and today()), SystemClock, FrozenClock,
default_timezone(), and local_date()/combine_to_instant() helpers.

Per ADR-0002: every module that touches "now" goes through Clock, not
datetime.now() directly. Tests pin time via FrozenClock (or freezegun
where Clock is used internally).
"""
from __future__ import annotations

from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo

import pytest

from ksma.shared.time import (
    Clock,
    FrozenClock,
    SystemClock,
    clock,
    combine_to_instant,
    default_timezone,
    local_date,
    reset_clock,
    set_clock,
)


class TestSystemClock:
    def setup_method(self) -> None:
        reset_clock()

    def test_now_returns_aware_utc(self) -> None:
        c = SystemClock()
        n = c.now()
        assert n.tzinfo is not None
        assert n.utcoffset().total_seconds() == 0

    def test_today_returns_date(self) -> None:
        c = SystemClock()
        assert isinstance(c.today(), date)


class TestFrozenClock:
    def test_pins_time(self) -> None:
        fixed = datetime(2026, 10, 20, 12, 0, 0, tzinfo=timezone.utc)
        c = FrozenClock(fixed)
        assert c.now() == fixed
        assert c.today() == date(2026, 10, 20)

    def test_naive_fixed_gets_utc_attached(self) -> None:
        naive = datetime(2026, 10, 20, 12, 0, 0)
        c = FrozenClock(naive)
        n = c.now()
        assert n.tzinfo == timezone.utc
        assert n.year == 2026


class TestProcessClock:
    def test_default_clock_is_system(self) -> None:
        reset_clock()
        assert isinstance(clock(), SystemClock)

    def test_set_clock_swaps(self) -> None:
        fixed = FrozenClock(datetime(2026, 1, 1, tzinfo=timezone.utc))
        set_clock(fixed)
        try:
            assert clock() is fixed
        finally:
            reset_clock()


class TestTimezone:
    def test_default_timezone_is_asia_jakarta(self) -> None:
        tz = default_timezone()
        assert tz == ZoneInfo("Asia/Jakarta")

    def test_local_date_from_naive_date(self) -> None:
        d = date(2026, 10, 20)
        assert local_date(d) == d

    def test_local_date_from_utc_datetime_in_jakarta(self) -> None:
        # 2026-10-20 17:00 UTC == 2026-10-21 00:00 Asia/Jakarta (no DST).
        utc = datetime(2026, 10, 20, 17, 0, 0, tzinfo=timezone.utc)
        assert local_date(utc) == date(2026, 10, 21)

    def test_combine_to_instant(self) -> None:
        instant = combine_to_instant(date(2026, 10, 20), datetime.min.time())
        assert instant.tzinfo == ZoneInfo("Asia/Jakarta")
        assert instant.year == 2026 and instant.month == 10 and instant.day == 20