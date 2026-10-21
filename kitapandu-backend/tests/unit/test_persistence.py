"""Unit tests for the shared persistence seam (engine / sessionmaker / GUCs).

Seam: ``ksma.shared.persistence``. Public surface:
- ``engine()`` — lazily-built, process-wide ``AsyncEngine`` with
  ``pool_pre_ping=True`` (stale pooled connections must be recycled before
  they are handed to a request).
- ``sessionmaker()`` — lazily-built, process-wide ``async_sessionmaker`` with
  ``expire_on_commit=False`` and ``autoflush=False`` (the BC layers flush
  explicitly; implicit autoflush would reorder the outbox/audit INSERTs).
- ``session_scope()`` — commit on clean exit, rollback on exception.
- ``set_current_tenant`` / ``set_current_unit`` / ``set_current_actor`` —
  the ``SET LOCAL`` RLS GUC preamble; ``bypass`` defaults to ``False``.
- ``dispose_engine()`` / ``reset_for_tests()``.

The module caches its engine in a module global, so every test here resets
that cache (``reset_for_tests``) and stubs the SQLAlchemy factories on the
module namespace. No DB, no network: the assertions are on the *kwargs handed
to ``create_async_engine`` / ``async_sessionmaker`` and on the SQL text sent
over the (fake) connection.
"""
from __future__ import annotations

import asyncio
from typing import Any
from uuid import uuid4

import pytest

from ksma.shared import persistence


# ---------------------------------------------------------------------------
# Doubles
# ---------------------------------------------------------------------------


class _FakeSettings:
    postgres_dsn = "postgresql+asyncpg://ksma:secret@db.test:5432/ksma"


class _FakeEngine:
    """Records ``aclose()`` calls; the attribute is how we prove teardown ran."""

    def __init__(self, dsn: str, kwargs: dict[str, Any]) -> None:
        self.dsn = dsn
        self.kwargs = kwargs
        self.closed = 0

    async def aclose(self) -> None:
        self.closed += 1


class _FakeSessionMaker:
    def __init__(self, bind: Any, kwargs: dict[str, Any]) -> None:
        self.bind = bind
        self.kwargs = kwargs


class _FakeConnection:
    def __init__(self) -> None:
        self.executed: list[tuple[str, dict[str, Any] | None]] = []

    async def execute(self, statement: Any, params: Any = None) -> None:
        self.executed.append((str(statement), params))


class _FakeSessionWithConn:
    def __init__(self) -> None:
        self.conn = _FakeConnection()

    async def connection(self) -> _FakeConnection:
        return self.conn


@pytest.fixture(autouse=True)
def _clear_engine_cache():
    persistence.reset_for_tests()
    yield
    persistence.reset_for_tests()


# ---------------------------------------------------------------------------
# engine()
# ---------------------------------------------------------------------------


def test_engine_builds_once_with_pool_pre_ping_and_pool_sizing(monkeypatch) -> None:
    """The engine is created lazily, exactly once, with the pool contract."""
    created: list[tuple[str, dict[str, Any]]] = []

    def fake_create_async_engine(dsn: str, **kwargs: Any) -> _FakeEngine:
        created.append((dsn, kwargs))
        return _FakeEngine(dsn, kwargs)

    monkeypatch.setattr(persistence, "get_settings", lambda: _FakeSettings())
    monkeypatch.setattr(persistence, "create_async_engine", fake_create_async_engine)

    first = persistence.engine()
    second = persistence.engine()

    assert first is second, "engine() must cache the process-wide engine"
    assert len(created) == 1
    dsn, kwargs = created[0]
    assert dsn == _FakeSettings.postgres_dsn
    assert kwargs["pool_pre_ping"] is True, "pool_pre_ping guards stale connections"
    assert kwargs["pool_size"] == 10
    assert kwargs["max_overflow"] == 10


def test_engine_reads_settings_lazily_not_at_import_time(monkeypatch) -> None:
    """A second ``reset_for_tests()`` rebuilds the engine from the *current*
    settings — i.e. the engine is not captured at module import."""
    seen: list[str] = []

    class _MutableSettings:
        def __init__(self, dsn: str) -> None:
            self.postgres_dsn = dsn

    def fake_get_settings() -> _MutableSettings:
        dsn = f"postgresql+asyncpg://u:p@host/{len(seen)}"
        seen.append(dsn)
        return _MutableSettings(dsn)

    monkeypatch.setattr(persistence, "get_settings", fake_get_settings)
    monkeypatch.setattr(
        persistence,
        "create_async_engine",
        lambda dsn, **kw: _FakeEngine(dsn, kw),
    )

    first = persistence.engine()
    persistence.reset_for_tests()
    second = persistence.engine()

    assert seen == [
        "postgresql+asyncpg://u:p@host/0",
        "postgresql+asyncpg://u:p@host/1",
    ]
    assert first is not second
    assert first.dsn.endswith("/0")
    assert second.dsn.endswith("/1")


# ---------------------------------------------------------------------------
# sessionmaker()
# ---------------------------------------------------------------------------


def test_sessionmaker_disables_expire_on_commit_and_autoflush(monkeypatch) -> None:
    """``expire_on_commit=False`` + ``autoflush=False`` are the contract: BC
    layers return ORM objects after commit and flush explicitly."""
    created: list[tuple[Any, dict[str, Any]]] = []
    engine_double = _FakeEngine("dsn", {})

    def fake_async_sessionmaker(bind: Any, **kwargs: Any) -> _FakeSessionMaker:
        created.append((bind, kwargs))
        return _FakeSessionMaker(bind, kwargs)

    monkeypatch.setattr(persistence, "engine", lambda: engine_double)
    monkeypatch.setattr(persistence, "async_sessionmaker", fake_async_sessionmaker)

    first = persistence.sessionmaker()
    second = persistence.sessionmaker()

    assert first is second, "sessionmaker() must cache the process-wide factory"
    assert len(created) == 1
    bind, kwargs = created[0]
    assert bind is engine_double
    assert kwargs["expire_on_commit"] is False
    assert kwargs["autoflush"] is False


# ---------------------------------------------------------------------------
# set_current_tenant — the RLS GUC preamble
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_set_current_tenant_defaults_to_rls_enforced() -> None:
    """``bypass`` defaults to ``False``: emit ``app.bypass_rls = 'false'`` plus
    the tenant GUC, and never the bypass GUC."""
    session = _FakeSessionWithConn()
    tenant_id = uuid4()

    await persistence.set_current_tenant(session, tenant_id)

    statements = [sql for sql, _ in session.conn.executed]
    assert statements[0] == "SET LOCAL app.bypass_rls = 'false'"
    assert statements.count(
        f"SET LOCAL app.current_tenant = '{tenant_id}'"
    ) == len(statements) - 1
    assert all("bypass_rls = 'true'" not in s for s in statements)


@pytest.mark.asyncio
async def test_set_current_tenant_bypass_true_skips_the_tenant_guc() -> None:
    """Boundary: ``bypass=True`` emits ONLY the bypass GUC (used by migrations
    and background workers, which are not tenant-scoped)."""
    session = _FakeSessionWithConn()
    tenant_id = uuid4()

    await persistence.set_current_tenant(session, tenant_id, bypass=True)

    statements = [sql for sql, _ in session.conn.executed]
    assert statements == ["SET LOCAL app.bypass_rls = 'true'"]


@pytest.mark.asyncio
async def test_set_current_tenant_later_call_overwrites_the_guc() -> None:
    """``SET LOCAL`` is transaction-scoped: calling twice must re-emit the GUC
    with the NEW tenant (later calls overwrite)."""
    session = _FakeSessionWithConn()
    first_tenant, second_tenant = uuid4(), uuid4()

    await persistence.set_current_tenant(session, first_tenant)
    await persistence.set_current_tenant(session, second_tenant)

    statements = [sql for sql, _ in session.conn.executed]
    assert f"SET LOCAL app.current_tenant = '{first_tenant}'" in statements
    assert statements[-1] == f"SET LOCAL app.current_tenant = '{second_tenant}'"


# ---------------------------------------------------------------------------
# dispose_engine() — teardown across the running/stopped-loop boundary
# ---------------------------------------------------------------------------


class _StoppedLoop:
    """An event loop object that exists but is not running."""

    def is_running(self) -> bool:
        return False

    def create_task(self, coro: Any) -> None:  # pragma: no cover - must not run
        coro.close()
        raise AssertionError("must not schedule a task on a non-running loop")


class _RunningLoop:
    def __init__(self) -> None:
        self.scheduled: list[Any] = []

    def is_running(self) -> bool:
        return True

    def create_task(self, coro: Any) -> None:
        self.scheduled.append(coro)


def _install_engine() -> _FakeEngine:
    engine_double = _FakeEngine("dsn", {})
    persistence._engine = engine_double  # noqa: SLF001 - module cache under test
    persistence._sessionmaker = _FakeSessionMaker(engine_double, {})  # noqa: SLF001
    return engine_double


def test_dispose_engine_runs_aclose_when_the_loop_is_not_running(monkeypatch) -> None:
    """Boundary pair A: loop present but STOPPED -> ``asyncio.run`` path.
    ``and -> or`` would wrongly schedule on the dead loop."""
    engine_double = _install_engine()

    with monkeypatch.context() as mp:
        mp.setattr(asyncio, "get_event_loop", lambda: _StoppedLoop())
        persistence.dispose_engine()

    assert engine_double.closed == 1
    assert persistence._engine is None  # noqa: SLF001
    assert persistence._sessionmaker is None  # noqa: SLF001


def test_dispose_engine_runs_aclose_when_there_is_no_loop(monkeypatch) -> None:
    """Boundary pair B: ``get_event_loop`` raises (no current loop in a
    worker thread) -> ``loop = None`` -> same ``asyncio.run`` path."""

    def _no_loop():
        raise RuntimeError("no current event loop")

    engine_double = _install_engine()

    with monkeypatch.context() as mp:
        mp.setattr(asyncio, "get_event_loop", _no_loop)
        persistence.dispose_engine()

    assert engine_double.closed == 1
    assert persistence._engine is None  # noqa: SLF001


def test_dispose_engine_schedules_aclose_on_a_running_loop(monkeypatch) -> None:
    """Boundary pair C: a RUNNING loop cannot be re-entered by
    ``asyncio.run``; the teardown must be scheduled as a task instead."""
    engine_double = _install_engine()
    running = _RunningLoop()

    with monkeypatch.context() as mp:
        mp.setattr(asyncio, "get_event_loop", lambda: running)
        persistence.dispose_engine()

    assert len(running.scheduled) == 1
    # The task was *scheduled*, not awaited: nothing closed synchronously.
    assert engine_double.closed == 0
    assert persistence._engine is None  # noqa: SLF001
    running.scheduled[0].close()


@pytest.mark.asyncio
async def test_dispose_engine_task_actually_closes_the_engine(monkeypatch) -> None:
    """The scheduled task really runs ``aclose()`` when the loop turns."""
    engine_double = _install_engine()
    running = _RunningLoop()

    with monkeypatch.context() as mp:
        mp.setattr(asyncio, "get_event_loop", lambda: running)
        persistence.dispose_engine()

    await running.scheduled[0]
    assert engine_double.closed == 1


def test_dispose_engine_is_a_noop_when_no_engine_was_built() -> None:
    """``dispose_engine`` on a cold module must not raise (no aclose to call)."""
    assert persistence._engine is None  # noqa: SLF001
    persistence.dispose_engine()  # must not raise
    assert persistence._sessionmaker is None  # noqa: SLF001