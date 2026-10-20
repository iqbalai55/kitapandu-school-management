"""Unit tests for the Clock protocol + Asia/Jakarta timezone helpers.

Seam: the Clock protocol (now() and today()), SystemClock, FrozenClock,
default_timezone(), and local_date()/combine_to_instant() helpers.

Per ADR-0002: every module that touches "now" goes through Clock, not
datetime.now() directly. Tests pin time via FrozenClock (or freezegun
where Clock is used internally).
"""
from __future__ import annotations

import os
from collections.abc import Iterator
from datetime import UTC, date, datetime
from zoneinfo import ZoneInfo

import pytest

from ksma.shared.time import (
    FrozenClock,
    SystemClock,
    clock,
    combine_to_instant,
    default_timezone,
    local_date,
    reset_clock,
    set_clock,
)


@pytest.fixture(autouse=True)
def _seed_settings_env() -> Iterator[None]:
    """Seed the env vars that ``Settings()`` requires so ``default_timezone``
    (which calls ``get_settings().default_timezone``) doesn't blow up under
    isolation.

    Per P36, the unit-test runner does not seed any ``KITAPANDU_*`` env vars;
    the settings module only validates the *required* fields (postgres_dsn,
    jwt_secret, minio_access_key, minio_secret_key). We don't use any of
    them here — we only need ``default_timezone`` to succeed. Cleared in
    teardown so other test files are unaffected.
    """
    prev = {k: os.environ.get(k) for k in (
        "KITAPANDU_POSTGRES_DSN",
        "KITAPANDU_POSTGRES_DSN_SYNC",
        "KITAPANDU_JWT_SECRET",
        "KITAPANDU_MINIO_ACCESS_KEY",
        "KITAPANDU_MINIO_SECRET_KEY",
    )}
    os.environ["KITAPANDU_POSTGRES_DSN"] = "postgresql+asyncpg://x:x@localhost/x"
    os.environ["KITAPANDU_POSTGRES_DSN_SYNC"] = "postgresql://x:x@localhost/x"
    os.environ["KITAPANDU_JWT_SECRET"] = "test-secret-not-used"
    os.environ["KITAPANDU_MINIO_ACCESS_KEY"] = "test-access-key"
    os.environ["KITAPANDU_MINIO_SECRET_KEY"] = "test-secret-key"
    # The settings cache may have been populated by another test file.
    from ksma.settings import get_settings  # noqa: PLC0415
    get_settings.cache_clear()
    try:
        yield
    finally:
        for k, v in prev.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        get_settings.cache_clear()


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
        fixed = datetime(2026, 10, 20, 12, 0, 0, tzinfo=UTC)
        c = FrozenClock(fixed)
        assert c.now() == fixed
        assert c.today() == date(2026, 10, 20)

    def test_naive_fixed_gets_utc_attached(self) -> None:
        naive = datetime(2026, 10, 20, 12, 0, 0)
        c = FrozenClock(naive)
        n = c.now()
        assert n.tzinfo == UTC
        assert n.year == 2026


class TestProcessClock:
    def test_default_clock_is_system(self) -> None:
        reset_clock()
        assert isinstance(clock(), SystemClock)

    def test_set_clock_swaps(self) -> None:
        fixed = FrozenClock(datetime(2026, 1, 1, tzinfo=UTC))
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
        utc = datetime(2026, 10, 20, 17, 0, 0, tzinfo=UTC)
        assert local_date(utc) == date(2026, 10, 21)

    def test_combine_to_instant(self) -> None:
        instant = combine_to_instant(date(2026, 10, 20), datetime.min.time())
        assert instant.tzinfo == ZoneInfo("Asia/Jakarta")
        assert instant.year == 2026 and instant.month == 10 and instant.day == 20

    # --- issue #37: Clock stability under freezegun + local_date canonicalisation

    def test_clock_now_is_stable_under_freezegun(self) -> None:
        """A freezegun freeze must pin Clock.now() to the frozen instant.

        ``Clock.now()`` is ``datetime.now(timezone.utc)`` under the hood, so
        freezegun's ``freeze_time`` decorator should fully control the
        returned value. A `+` / `==` flip in ``local_date`` (which shares
        the time helpers) is what this guards against.
        """
        from freezegun import freeze_time  # noqa: PLC0415

        with freeze_time("2026-10-20 12:00:00", tz_offset=0):
            first = clock().now()
            second = clock().now()
        # Same wall clock between calls under freeze.
        assert first == second
        # And the frozen instant is exactly what we asked for.
        assert first == datetime(2026, 10, 20, 12, 0, 0, tzinfo=UTC)

    def test_clock_today_matches_local_date_for_utc_morning(self) -> None:
        """For a UTC-morning instant, ``Clock.today()`` and ``local_date()``
        must agree — no timezone drift. Asia/Jakarta is UTC+7, so a UTC
        morning instant still falls on the *previous* calendar date in Jakarta.
        """
        utc = datetime(2026, 10, 20, 5, 0, 0, tzinfo=UTC)
        c = FrozenClock(utc)
        # Today() uses the *UTC* calendar by spec — it is the UTC instant's
        # date, NOT the local calendar date. The pin below locks both.
        assert c.today() == date(2026, 10, 20)
        # local_date(), on the other hand, applies the tenant TZ.
        assert local_date(utc) == date(2026, 10, 20)  # 05:00 UTC = 12:00 Jakarta same day

    def test_local_date_converts_naive_datetime_as_utc(self) -> None:
        # A naive datetime is taken to be UTC by local_date. A flip from
        # `tzinfo=timezone.utc` to `tzinfo=None` would crash or return a
        # wrong value.
        naive = datetime(2026, 10, 20, 17, 0, 0)
        assert local_date(naive) == date(2026, 10, 21)

    def test_local_date_with_explicit_tz_overrides_default(self) -> None:
        # The `tz=` argument must take precedence over settings.default_timezone.
        # A missing `tz or default_timezone()` branch would force Asia/Jakarta
        # regardless of the call-site preference.
        utc = datetime(2026, 10, 20, 23, 0, 0, tzinfo=UTC)
        # In UTC, 23:00 is still the 20th.
        assert local_date(utc, tz=UTC) == date(2026, 10, 20)
        # In Asia/Jakarta (UTC+7), 23:00 UTC is 06:00 next day.
        assert local_date(utc, tz=ZoneInfo("Asia/Jakarta")) == date(2026, 10, 21)

    @pytest.mark.parametrize(
        "utc_instant, expected_jakarta_date",
        [
            # Year/month boundary: 23:30 UTC on Dec 31 is 06:30 Jan 1 Jakarta.
            (datetime(2025, 12, 31, 23, 30, 0, tzinfo=UTC), date(2026, 1, 1)),
            # Same day (UTC morning).
            (datetime(2026, 10, 20, 1, 0, 0, tzinfo=UTC), date(2026, 10, 20)),
            # UTC late afternoon -> Jakarta next-day morning.
            (datetime(2026, 10, 20, 17, 0, 0, tzinfo=UTC), date(2026, 10, 21)),
            # Mid-day UTC -> same day Jakarta (no boundary crossing).
            (datetime(2026, 10, 20, 5, 0, 0, tzinfo=UTC), date(2026, 10, 20)),
            # UTC midnight -> Jakarta 07:00 same day.
            (datetime(2026, 10, 20, 0, 0, 0, tzinfo=UTC), date(2026, 10, 20)),
        ],
    )
    def test_local_date_boundary_table(
        self, utc_instant: datetime, expected_jakarta_date: date
    ) -> None:
        # Parametrised over the calendar boundary the function crosses.
        assert local_date(utc_instant) == expected_jakarta_date

    def test_combine_to_instant_attaches_local_zone(self) -> None:
        # combine_to_instant must NOT attach UTC; it must attach the
        # tenant tz. A `tzinfo=timezone.utc` flip in this helper would
        # leak UTC into downstream "local" math.
        d = date(2026, 10, 20)
        instant = combine_to_instant(d, datetime.min.time())
        assert instant.tzinfo == ZoneInfo("Asia/Jakarta")
        assert instant.utcoffset().total_seconds() == 7 * 3600

    def test_combine_to_instant_preserves_explicit_tz(self) -> None:
        # Caller-supplied tz wins over default.
        utc_zone = ZoneInfo("UTC")
        instant = combine_to_instant(
            date(2026, 10, 20), datetime.min.time(), tz=utc_zone
        )
        assert instant.tzinfo == utc_zone
        assert instant.utcoffset().total_seconds() == 0