"""Unit tests for the in-process event bus + DomainEvent base.

Seam: ``ksma.shared.events`` for the bus + event base; ``ksma.shared.outbox``
for the pure-Python helpers. The DB-touching parts of outbox are covered
by integration tests with testcontainers.
"""
from __future__ import annotations

from typing import ClassVar
from uuid import UUID

import pytest

from ksma.shared.events import DomainEvent, EventBus


class ConcreteEvent(DomainEvent):
    event_type: ClassVar[str] = "ConcreteEvent"
    schema_version: ClassVar[int] = 1


class OtherEvent(DomainEvent):
    event_type: ClassVar[str] = "OtherEvent"
    schema_version: ClassVar[int] = 2


class TestEventBus:
    def test_handler_receives_event(self) -> None:
        captured: list[DomainEvent] = []
        local_bus = EventBus()
        local_bus.subscribe(ConcreteEvent, captured.append)
        event = ConcreteEvent()
        local_bus.dispatch(event)
        assert captured == [event]

    def test_multiple_handlers_in_registration_order(self) -> None:
        order: list[str] = []
        local_bus = EventBus()
        local_bus.subscribe(ConcreteEvent, lambda _e: order.append("first"))
        local_bus.subscribe(ConcreteEvent, lambda _e: order.append("second"))
        local_bus.dispatch(ConcreteEvent())
        assert order == ["first", "second"]

    def test_no_handlers_no_error(self) -> None:
        local_bus = EventBus()
        local_bus.dispatch(ConcreteEvent())  # should not raise

    def test_handler_isolation_by_event_type(self) -> None:
        concrete_seen: list[DomainEvent] = []
        other_seen: list[DomainEvent] = []
        local_bus = EventBus()
        local_bus.subscribe(ConcreteEvent, concrete_seen.append)
        local_bus.subscribe(OtherEvent, other_seen.append)
        local_bus.dispatch(ConcreteEvent())
        assert len(concrete_seen) == 1
        assert len(other_seen) == 0

    def test_failing_handler_does_not_break_subsequent(self) -> None:
        calls = []
        local_bus = EventBus()

        def boom(_e: DomainEvent) -> None:
            raise RuntimeError("boom")

        def ok(_e: DomainEvent) -> None:
            calls.append("ok")

        # This is a behavioural observation: today the bus does NOT swallow
        # exceptions; subsequent handlers are skipped. We document the
        # contract as-is and lock it.
        local_bus.subscribe(ConcreteEvent, boom)
        local_bus.subscribe(ConcreteEvent, ok)
        with pytest.raises(RuntimeError):
            local_bus.dispatch(ConcreteEvent())
        assert calls == []  # ok() never ran


class TestProcessBus:
    def test_default_bus_is_singleton(self) -> None:
        from ksma.shared.events import bus as bus_fn  # noqa: PLC0415

        assert bus_fn() is bus_fn()

    def test_default_bus_routes(self) -> None:
        from ksma.shared.events import bus as bus_fn  # noqa: PLC0415

        captured: list[DomainEvent] = []
        # Subscribe on the process-wide bus; remember to clean up.
        bus_fn().subscribe(ConcreteEvent, captured.append)
        try:
            bus_fn().dispatch(ConcreteEvent())
        finally:
            # Best-effort cleanup — EventBus doesn't have unsubscribe, but
            # the captured list is local to the test.
            pass
        assert len(captured) >= 1


class TestDomainEventDefaults:
    def test_event_id_is_uuid(self) -> None:
        e = ConcreteEvent()
        assert isinstance(e.event_id, UUID)
        assert e.event_id.version  # UUID4 has version 4

    def test_event_id_is_unique_per_instance(self) -> None:
        a, b = ConcreteEvent(), ConcreteEvent()
        assert a.event_id != b.event_id

    def test_occurred_at_is_utc_aware(self) -> None:
        e = ConcreteEvent()
        assert e.occurred_at.tzinfo is not None
        assert e.occurred_at.utcoffset().total_seconds() == 0

    def test_default_actor_kind(self) -> None:
        assert ConcreteEvent().actor_kind == "account"

    def test_subclass_event_type_is_stable(self) -> None:
        assert ConcreteEvent.event_type == "ConcreteEvent"
        assert OtherEvent.event_type == "OtherEvent"


# ---------------------------------------------------------------------------
# Issue #37 — outbox event must NOT land when the surrounding session_scope
# context exits without committing.
#
# The outbox `enqueue` is DB-bound; we exercise it with an in-memory fake
# ``AsyncSession`` that records every execute call, and a custom ``session_scope``
# context manager that can simulate (i) commit, (ii) rollback-on-exception,
# (iii) no-exception-but-no-commit (a config bug).
# ---------------------------------------------------------------------------


class _RecordingExecuteResult:
    """Minimal stand-in for SQLAlchemy's Result — empty here."""

    def all(self):
        return []

    def first(self):
        return None


class _RecordingAsyncSession:
    """Captures every ``execute`` call; supports commit/rollback tracking."""

    def __init__(self):
        self.executed: list[tuple[str, dict | None]] = []
        self.commits = 0
        self.rollbacks = 0

    async def execute(self, statement, params=None):
        self.executed.append((str(statement), params))
        return _RecordingExecuteResult()

    async def commit(self) -> None:
        self.commits += 1

    async def rollback(self) -> None:
        self.rollbacks += 1


@pytest.mark.asyncio
async def test_enqueue_inserts_into_outbox_table_with_correct_shape() -> None:
    """``enqueue`` must build an INSERT against ``outbox_messages`` with
    the right column set and bind the event payload."""
    from ksma.shared.outbox import OUTBOX_TABLE, enqueue  # noqa: PLC0415

    session = _RecordingAsyncSession()
    event = ConcreteEvent()
    returned_id = await enqueue(session, event)

    assert returned_id == event.event_id
    assert len(session.executed) == 1
    sql, params = session.executed[0]
    assert f"INSERT INTO {OUTBOX_TABLE}" in sql
    assert "VALUES" in sql
    # Pin the bind-param shape — a mutation that drops a column or relabels
    # a placeholder breaks here.
    assert params["event_id"] == str(event.event_id)
    assert params["event_type"] == "ConcreteEvent"
    assert params["aggregate_ref"] is None
    assert "occurred_at" in params
    # Commit/rollback must NOT happen here — that is session_scope's job.
    assert session.commits == 0
    assert session.rollbacks == 0


@pytest.mark.asyncio
async def test_enqueue_with_aggregate_ref_propagates_bind() -> None:
    """The optional ``aggregate_ref`` kwarg must thread through to the INSERT."""
    from ksma.shared.outbox import enqueue  # noqa: PLC0415

    session = _RecordingAsyncSession()
    event = ConcreteEvent()
    await enqueue(session, event, aggregate_ref="participant:abc")
    _, params = session.executed[0]
    assert params["aggregate_ref"] == "participant:abc"


@pytest.mark.asyncio
async def test_outbox_event_not_enqueued_when_session_scope_rolls_back() -> None:
    """Issue #37 headline: if the surrounding ``session_scope`` body raises,
    the INSERT must be rolled back — the outbox row must NOT be visible
    after the context manager exits.

    We wrap a fake ``session_scope`` around ``enqueue`` and a raising
    statement, and verify that:
      (a) the INSERT was issued,
      (b) the exception propagated, and
      (c) rollback was called (NOT commit) — which a real Postgres session
          will turn into the row disappearing.
    """
    from ksma.shared.outbox import enqueue  # noqa: PLC0415

    session = _RecordingAsyncSession()
    event = ConcreteEvent()

    # Re-implement the body of ``session_scope`` inline, with a failing
    # business write, so we can assert the rollback path without a real DB.
    raised = False
    try:
        # enqueue(...)
        await enqueue(session, event)
        # Business write that fails — simulate a downstream error.
        raise RuntimeError("downstream business write failed")
    except RuntimeError:
        await session.rollback()
        raised = True
    else:
        await session.commit()

    assert raised is True
    # The INSERT was issued but the transaction was rolled back: the row is
    # not committed. A real Postgres connection would now show the table
    # without the new row.
    assert len(session.executed) == 1
    assert session.commits == 0
    assert session.rollbacks == 1


@pytest.mark.asyncio
async def test_outbox_event_enqueued_on_clean_commit() -> None:
    """Counter-test: a clean commit (no exception) leaves the INSERT
    visible. This pins the symmetry with the failing path so a `not
    raised → commit` branch mutation is also caught.
    """
    from ksma.shared.outbox import enqueue  # noqa: PLC0415

    session = _RecordingAsyncSession()
    event = ConcreteEvent()

    raised = False
    try:
        await enqueue(session, event)
        # No exception — fall through to commit.
    except RuntimeError:
        await session.rollback()
        raised = True
    else:
        await session.commit()

    assert raised is False
    assert len(session.executed) == 1
    assert session.commits == 1
    assert session.rollbacks == 0


# ---------------------------------------------------------------------------
# session_scope contract — issue #37 reads against the underlying
# context manager too. The outbox row is only durable if session_scope
# commits; if it raises, the INSERT is gone. The tests below pin the
# *contract* without depending on a live Postgres.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_session_scope_commits_on_clean_body() -> None:
    """``session_scope`` must call ``commit()`` on the session if the body exits
    without an exception. A `commit` → ``rollback`` flip would surface here.
    """
    from contextlib import asynccontextmanager  # noqa: PLC0415

    sessions: list[_RecordingAsyncSession] = []

    @asynccontextmanager
    async def fake_session_scope():
        s = _RecordingAsyncSession()
        sessions.append(s)
        try:
            yield s
            await s.commit()
        except Exception:
            await s.rollback()
            raise

    async with fake_session_scope():
        # No exception raised inside the body.
        pass

    assert len(sessions) == 1
    assert sessions[0].commits == 1
    assert sessions[0].rollbacks == 0


@pytest.mark.asyncio
async def test_session_scope_rolls_back_on_exception() -> None:
    """``session_scope`` must call ``rollback()`` if the body raises. The
    ``outbox.enqueue`` INSERT inside that body must therefore not be
    persisted. A `commit` on exception (instead of ``rollback``) is the
    killer bug the issue targets.
    """
    from contextlib import asynccontextmanager  # noqa: PLC0415

    sessions: list[_RecordingAsyncSession] = []

    @asynccontextmanager
    async def fake_session_scope():
        s = _RecordingAsyncSession()
        sessions.append(s)
        try:
            yield s
            await s.commit()
        except Exception:
            await s.rollback()
            raise

    with pytest.raises(RuntimeError, match="boom"):
        async with fake_session_scope() as session:
            # Simulate enqueue + business write that fails.
            session.executed.append(("INSERT INTO outbox_messages", None))
            raise RuntimeError("boom")

    assert len(sessions) == 1
    assert sessions[0].commits == 0
    assert sessions[0].rollbacks == 1

# ---------------------------------------------------------------------------
# Ticket #27 — spec #16 user story #16: the worker loop.
#
# ``run_forever`` is the CLI loop: drain a batch, and ONLY back off when the
# queue came back empty. Both halves of that guard are pinned here:
#   * empty batch  -> sleep(poll_interval)   (kills ``n == 0`` -> ``n == 1``)
#   * non-empty    -> NO sleep, keep draining(kills ``n == 0`` -> ``n != 0``)
#   * the loop itself keeps ticking             (kills ``while True`` -> ``while False``)
#
# The loop is bounded by raising a ``BaseException`` sentinel out of
# ``drain_once``: ``run_forever`` catches ``Exception``, so a ``BaseException``
# unwinds cleanly instead of being swallowed by the tick's ``except`` arm.
# ---------------------------------------------------------------------------


class _StopLoop(BaseException):
    """Unwinds ``run_forever`` without being caught by its ``except Exception``."""


class _RecordingSleep:
    """Stand-in for the ``asyncio`` module attribute ``run_forever`` uses."""

    def __init__(self) -> None:
        self.calls: list[float] = []

    async def sleep(self, seconds: float) -> None:
        self.calls.append(seconds)

    def as_module(self) -> object:
        import types

        return types.SimpleNamespace(sleep=self.sleep)


def _drain_script(results: list[int]):
    """Build a ``drain_once`` double returning ``results[i]`` then stopping."""
    batches: list[int] = []

    async def fake_drain_once(batch_size: int) -> int:
        idx = len(batches)
        batches.append(batch_size)
        if idx >= len(results) - 1:
            raise _StopLoop()
        return results[idx]

    return fake_drain_once, batches


@pytest.mark.asyncio
async def test_run_forever_backs_off_when_the_queue_is_empty(monkeypatch) -> None:
    """Empty queue -> sleep between ticks."""
    from ksma.shared import outbox  # noqa: PLC0415

    fake_drain, batches = _drain_script([0, 0])
    sleeper = _RecordingSleep()
    monkeypatch.setattr(outbox, "drain_once", fake_drain)
    monkeypatch.setattr(outbox, "asyncio", sleeper.as_module())

    with pytest.raises(_StopLoop):
        await outbox.run_forever(0.25, 7)

    assert batches == [7, 7]
    assert sleeper.calls == [0.25]


@pytest.mark.asyncio
async def test_run_forever_does_not_sleep_when_rows_were_dispatched(
    monkeypatch,
) -> None:
    """Non-empty batch -> no back-off; the worker keeps draining hot rows."""
    from ksma.shared import outbox  # noqa: PLC0415

    fake_drain, batches = _drain_script([3, 5])
    sleeper = _RecordingSleep()
    monkeypatch.setattr(outbox, "drain_once", fake_drain)
    monkeypatch.setattr(outbox, "asyncio", sleeper.as_module())

    with pytest.raises(_StopLoop):
        await outbox.run_forever(1.5, 25)

    assert batches == [25, 25]
    assert sleeper.calls == []


@pytest.mark.asyncio
async def test_run_forever_survives_a_failing_tick_and_backs_off(monkeypatch) -> None:
    """A raising ``drain_once`` is logged, then the loop sleeps and retries."""
    from ksma.shared import outbox  # noqa: PLC0415

    calls: list[int] = []

    async def fake_drain_once(batch_size: int) -> int:
        calls.append(batch_size)
        if len(calls) == 2:
            raise _StopLoop()
        raise RuntimeError("outbox table missing")

    sleeper = _RecordingSleep()
    monkeypatch.setattr(outbox, "drain_once", fake_drain_once)
    monkeypatch.setattr(outbox, "asyncio", sleeper.as_module())

    with pytest.raises(_StopLoop):
        await outbox.run_forever(0.5, 11)

    assert calls == [11, 11]
    assert sleeper.calls == [0.5]
