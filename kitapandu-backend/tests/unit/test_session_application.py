"""Unit tests for ``ksma.session.application`` — the session/closure/correction
seam, plus the journal entry point.

Seam
----
``ksma.session.application`` (R08 / R09 / V09). Public surface pinned here:

- ``close_session`` (BR04 + V09): R09 invariant checklist. The closure row
  carries the three ``all_attendance_filled`` / ``journal_topic_present`` /
  ``actual_instructor_present`` truth values; the lifecycle transitions to
  ``closed``; the completion ``outcome`` is ``COMPLETED``.
- ``correct_session`` (V09): empty ``correction_reason`` is rejected;
  ``attendance_changes=None`` is a valid no-op; a chained
  ``SessionCompletion`` with ``supersedes_id`` is created.
- ``add_revision`` (R08): rejected when the session is cancelled; rejected
  on ``ends_at <= starts_at``; the first revision dispatches
  ``SessionScheduled`` (vs ``SessionRescheduled``).
- ``cancel_session`` (BR04): lifecycle transitions to ``cancelled`` and
  ``closure_id`` stays ``None`` (the session is NOT counted as completed).
- ``write_journal``: empty ``topic`` is rejected; non-empty topic +
  ``actual_instructor_id`` are persisted.

Persistence is the in-memory ``FakeAsyncSession`` /
``FakeTenantContext`` toolkit from ``tests/unit/conftest_helpers.py``.
No DB, no Docker, no network.

What is NOT asserted here (lives in the integration tier):

- RLS policies and JSONB index selection.
- The real Postgres ``FOR UPDATE`` row lock.
- The outbox table; ``enqueue`` writes to the in-memory fake's INSERT log.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from unittest.mock import Mock
from uuid import UUID, uuid4

import pytest

from tests.unit.conftest_helpers import (
    FakeAsyncSession,
    FakeTenantContext,
)

from ksma.session.application import (
    add_revision,
    cancel_session,
    close_session,
    correct_attendance,
    correct_session,
    write_journal,
)
from ksma.session.domain import (
    CompletionDecision,
    SessionLifecycleStatus,
)
from ksma.shared.events import bus
from ksma.shared.rbac import RoleCode


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _ctx(tenant_id: UUID | None = None, **overrides: Any) -> Any:
    """Build an ACCOUNT ``TenantContext`` rooted at ``tenant_id``."""
    builder = FakeTenantContext(RoleCode.AC, tenant_id=tenant_id).with_account(uuid4(), uuid4())
    for name, value in overrides.items():
        builder = getattr(builder, name)(value)
    return builder.build()


def _seed_session(
    session: FakeAsyncSession,
    *,
    tenant_id: UUID,
    session_id: UUID | None = None,
    unit_id: UUID | None = None,
    lifecycle_status: SessionLifecycleStatus = SessionLifecycleStatus.SCHEDULED,
) -> UUID:
    """Seed a ``session`` row the application can ``lock_for_update``."""
    sid = session_id or uuid4()
    session.seed(
        "session",
        {
            "id": str(sid),
            "tenant_id": str(tenant_id),
            "unit_id": str(unit_id or uuid4()),
            "batch_id": str(uuid4()),
            "session_no": 1,
            "effective_revision_id": None,
            "lifecycle_status": lifecycle_status.value,
            "closure_id": None,
            "reschedule_reason": None,
            "created_at": None,
            "created_by": "tester",
            "updated_at": None,
            "updated_by": "tester",
        },
    )
    return sid


def _seed_journal(
    session: FakeAsyncSession,
    *,
    tenant_id: UUID,
    session_id: UUID,
    topic: str = "Algebra",
) -> None:
    """Seed a complete ``teaching_journal`` row for ``close_session``."""
    session.seed(
        "teaching_journal",
        {
            "id": str(uuid4()),
            "tenant_id": str(tenant_id),
            "session_id": str(session_id),
            "actual_instructor_id": str(uuid4()),
            "topic": topic,
            "activities": None,
            "internal_note": None,
            "portal_summary": None,
            "follow_up": None,
            "created_at": None,
            "created_by": "tester",
            "updated_at": None,
            "updated_by": "tester",
        },
    )


def _seed_completion_no_row(
    session: FakeAsyncSession,
    *,
    tenant_id: UUID,
    session_id: UUID,
    completion_no: int = 0,
) -> None:
    """Seed the ``n`` alias row ``SessionCompletionRepository.next_completion_no``
    reads. A single ``n`` key is enough — the application's
    ``(row.n or 0) + 1`` arithmetic only needs the MAX alias."""
    session.seed(
        "session_completion",
        {"n": completion_no, "session_id": str(session_id), "tenant_id": str(tenant_id)},
    )


def _seed_revision_no_row(
    session: FakeAsyncSession,
    *,
    tenant_id: UUID,
    session_id: UUID,
    revision_no: int = 0,
) -> None:
    """Seed the ``n`` alias row ``SessionRevisionRepository.next_revision_no``
    reads. The application's ``(row.n or 0) + 1`` arithmetic only needs the
    MAX alias."""
    session.seed(
        "session_revision",
        {"n": revision_no, "session_id": str(session_id), "tenant_id": str(tenant_id)},
    )


def _patch_enrollment_join(
    session: FakeAsyncSession,
    rows: list[dict[str, Any]],
) -> None:
    """Return ``rows`` from the ``FROM enrollment e LEFT JOIN attendance a``
    query inside ``close_session``.

    ``FakeAsyncSession`` cannot model the LEFT JOIN + subquery in the
    WHERE clause (its regex-based filter misreads the
    ``SELECT batch_id FROM session WHERE id = :sid`` subquery as a
    top-level ``id = :sid`` predicate). This patches the single SQL
    affected and routes everything else through the fake.
    """
    from tests.unit.conftest_helpers import FakeResult, FakeRow

    orig_execute = session.execute

    async def _execute(
        stmt: Any, params: Any | None = None, *args: Any, **kwargs: Any
    ) -> Any:
        sql_text = str(stmt) if not isinstance(stmt, str) else stmt
        if "FROM enrollment e" in sql_text and "LEFT JOIN attendance" in sql_text:
            return FakeResult([FakeRow(dict(r)) for r in rows])
        return await orig_execute(stmt, params, *args, **kwargs)

    session.execute = _execute  # type: ignore[method-assign]


@pytest.fixture(autouse=True)
def _clear_event_bus() -> Any:
    """Reset the in-process event bus so subscribers don't bleed across tests."""
    saved_handlers = dict(bus()._handlers)  # type: ignore[attr-defined]
    bus()._handlers.clear()  # type: ignore[attr-defined]
    try:
        yield
    finally:
        bus()._handlers.clear()  # type: ignore[attr-defined]
        bus()._handlers.update(saved_handlers)  # type: ignore[attr-defined]


# ===========================================================================
# close_session (BR04 + V09)
# ===========================================================================


class TestCloseSession:
    """``close_session`` — R09 closure invariants and BR04 lifecycle."""

    async def test_session_rejects_cancelled_lifecycle(self) -> None:
        """A cancelled session is rejected before the closure checklist runs."""
        from ksma.shared.errors import StateConflictError

        session = FakeAsyncSession()
        tenant_id = uuid4()
        sid = _seed_session(
            session,
            tenant_id=tenant_id,
            lifecycle_status=SessionLifecycleStatus.CANCELLED,
        )
        ctx = _ctx(tenant_id=tenant_id)
        with pytest.raises(StateConflictError):
            await close_session(session, ctx, session_id=sid)
        # No completion row was inserted.
        completion_rows = [
            c for c in session.calls
            if c.op == "execute" and "session_completion" in (c.sql or "")
        ]
        assert completion_rows == []

    async def test_session_rejects_already_closed(self) -> None:
        """A second close on an already-closed session is rejected."""
        from ksma.shared.errors import StateConflictError

        session = FakeAsyncSession()
        tenant_id = uuid4()
        sid = _seed_session(
            session,
            tenant_id=tenant_id,
            lifecycle_status=SessionLifecycleStatus.CLOSED,
        )
        ctx = _ctx(tenant_id=tenant_id)
        with pytest.raises(StateConflictError):
            await close_session(session, ctx, session_id=sid)

    async def test_completed_enrollment_skipped_in_missing_check(self) -> None:
        """An enrollment in ``completed`` status is skipped even without attendance.

        Catches ``== -> !=`` on the ``r.enrollment_status == "completed"``
        branch (line 325): a flipped operator would mark this enrollment
        as missing-attendance and the closure would fail.
        """
        from ksma.shared.errors import ValidationError

        session = FakeAsyncSession()
        tenant_id = uuid4()
        sid = _seed_session(
            session, tenant_id=tenant_id, lifecycle_status=SessionLifecycleStatus.SCHEDULED
        )
        # Join result: a completed enrollment with no attendance row -> the
        # ``completed`` branch must skip it instead of flagging it missing.
        _patch_enrollment_join(
            session,
            [
                {
                    "enrollment_id": str(uuid4()),
                    "enrollment_status": "completed",
                    "attendance_code": None,
                }
            ],
        )
        _seed_journal(session, tenant_id=tenant_id, session_id=sid, topic="Algebra")
        _seed_completion_no_row(session, tenant_id=tenant_id, session_id=sid)
        ctx = _ctx(tenant_id=tenant_id)
        result = await close_session(session, ctx, session_id=sid)
        assert result.outcome is CompletionDecision.COMPLETED

    async def test_unfilled_attendance_blocked(self) -> None:
        """An active enrollment with no attendance code blocks the closure.

        The fake can't model the ``LEFT JOIN attendance`` SQL (its WHERE
        filter misreads the subquery), so the enrollment row is supplied
        via an ``AsyncMock`` for the single ``FROM enrollment e`` call.
        """
        from ksma.shared.errors import ValidationError

        session = FakeAsyncSession()
        tenant_id = uuid4()
        sid = _seed_session(
            session, tenant_id=tenant_id, lifecycle_status=SessionLifecycleStatus.SCHEDULED
        )
        _seed_journal(session, tenant_id=tenant_id, session_id=sid, topic="Algebra")
        ctx = _ctx(tenant_id=tenant_id)
        # Inject a row into the enrollment LEFT JOIN result: an active
        # enrollment with no attendance code.
        _patch_enrollment_join(
            session,
            [
                {"enrollment_id": str(uuid4()), "enrollment_status": "active", "attendance_code": None}
            ],
        )
        with pytest.raises(ValidationError) as exc_info:
            await close_session(session, ctx, session_id=sid)
        assert "R09" in str(exc_info.value)

    async def test_missing_journal_blocks_closure(self) -> None:
        """A session with no teaching journal is rejected."""
        from ksma.shared.errors import ValidationError

        session = FakeAsyncSession()
        tenant_id = uuid4()
        sid = _seed_session(
            session, tenant_id=tenant_id, lifecycle_status=SessionLifecycleStatus.SCHEDULED
        )
        _patch_enrollment_join(
            session,
            [
                {
                    "enrollment_id": str(uuid4()),
                    "enrollment_status": "active",
                    "attendance_code": "hadir",
                }
            ],
        )
        ctx = _ctx(tenant_id=tenant_id)
        with pytest.raises(ValidationError) as exc_info:
            await close_session(session, ctx, session_id=sid)
        assert "TeachingJournal" in str(exc_info.value)

    async def test_journal_empty_topic_blocks_closure(self) -> None:
        """A journal whose ``topic`` is empty fails the R09 invariant.

        Pins the ``j is None or not j.topic`` short-circuit (line 343) so a
        ``or -> and`` flip on the empty-topic guard is caught.
        """
        from ksma.shared.errors import ValidationError

        session = FakeAsyncSession()
        tenant_id = uuid4()
        sid = _seed_session(
            session, tenant_id=tenant_id, lifecycle_status=SessionLifecycleStatus.SCHEDULED
        )
        _patch_enrollment_join(
            session,
            [
                {
                    "enrollment_id": str(uuid4()),
                    "enrollment_status": "active",
                    "attendance_code": "hadir",
                }
            ],
        )
        _seed_journal(session, tenant_id=tenant_id, session_id=sid, topic="")
        ctx = _ctx(tenant_id=tenant_id)
        with pytest.raises(ValidationError):
            await close_session(session, ctx, session_id=sid)

    async def test_checklist_snapshot_records_every_invariant(self) -> None:
        """The closure's checklist pins all three R09 truth values to True.

        Catches the ``True -> False`` flips on lines 352 / 353 / 354 — any
        one flips the closure record into a state that doesn't actually
        encode the invariants.
        """
        from ksma.session.domain.events import SessionClosed

        session = FakeAsyncSession()
        tenant_id = uuid4()
        sid = _seed_session(
            session, tenant_id=tenant_id, lifecycle_status=SessionLifecycleStatus.SCHEDULED
        )
        _patch_enrollment_join(
            session,
            [
                {
                    "enrollment_id": str(uuid4()),
                    "enrollment_status": "active",
                    "attendance_code": "hadir",
                }
            ],
        )
        _seed_journal(session, tenant_id=tenant_id, session_id=sid, topic="Algebra")
        _seed_completion_no_row(session, tenant_id=tenant_id, session_id=sid)
        # AsyncMock spy on the in-process bus — records SessionClosed
        # dispatch. The fake's INSERT log covers the outbox side.
        handler = Mock()
        bus().subscribe(SessionClosed, handler)
        ctx = _ctx(tenant_id=tenant_id)
        result = await close_session(session, ctx, session_id=sid)
        ck = result.checklist_snapshot
        assert ck["all_attendance_filled"] is True
        assert ck["journal_topic_present"] is True
        assert ck["actual_instructor_present"] is True
        assert ck["rule_version"] == "R09-v1"
        assert result.outcome is CompletionDecision.COMPLETED
        # The session row transitioned to CLOSED.
        updated = session.rows("session")[0]
        assert updated["lifecycle_status"] == SessionLifecycleStatus.CLOSED.value
        # And a completion row was inserted.
        completion_inserts = [
            c for c in session.calls
            if c.op == "execute"
            and "INSERT INTO session_completion" in (c.sql or "")
        ]
        assert len(completion_inserts) == 1
        # The SessionClosed event reached the in-process bus (sync Mock).
        assert handler.call_count == 1


# ===========================================================================
# correct_session (V09)
# ===========================================================================


class TestCorrectSession:
    """``correct_session`` — V09: correction_reason is required."""

    async def test_empty_correction_reason_rejected(self) -> None:
        """An empty ``correction_reason`` is a ValidationError (line 395)."""
        from ksma.shared.errors import ValidationError

        session = FakeAsyncSession()
        tenant_id = uuid4()
        sid = _seed_session(
            session,
            tenant_id=tenant_id,
            lifecycle_status=SessionLifecycleStatus.CLOSED,
        )
        ctx = _ctx(tenant_id=tenant_id)
        with pytest.raises(ValidationError) as exc_info:
            await correct_session(session, ctx, session_id=sid, correction_reason="")
        assert "correction_reason" in str(exc_info.value)

    async def test_rejects_non_closed_session(self) -> None:
        """Correction is only valid on a CLOSED session."""
        from ksma.shared.errors import StateConflictError

        session = FakeAsyncSession()
        tenant_id = uuid4()
        sid = _seed_session(
            session,
            tenant_id=tenant_id,
            lifecycle_status=SessionLifecycleStatus.SCHEDULED,
        )
        ctx = _ctx(tenant_id=tenant_id)
        with pytest.raises(StateConflictError):
            await correct_session(session, ctx, session_id=sid, correction_reason="oops")

    async def test_chains_new_completion_with_supersedes(self) -> None:
        """A correction creates a chained SessionCompletion with ``supersedes_id``.

        Pins the ``attendance_changes or []`` short-circuit (line 410) by
        passing ``None``: a flipped ``and`` operator would TypeError on
        ``None and []`` evaluation when ``attendance_changes=None``.
        """
        session = FakeAsyncSession()
        tenant_id = uuid4()
        sid = _seed_session(
            session,
            tenant_id=tenant_id,
            lifecycle_status=SessionLifecycleStatus.CLOSED,
        )
        # Seed the latest completion + the `n` alias row for next_completion_no.
        completion_id = uuid4()
        session.seed(
            "session_completion",
            {
                "id": str(completion_id),
                "tenant_id": str(tenant_id),
                "session_id": str(sid),
                "completion_no": 1,
                "n": 1,
                "decision": {"actor": "old"},
                "checklist_snapshot": {"rule_version": "R09-v1"},
                "policy_id": None,
                "supersedes_id": None,
                "correction_reason": None,
                "outcome": CompletionDecision.COMPLETED.value,
                "created_at": None,
                "created_by": "tester",
                "updated_at": None,
                "updated_by": "tester",
            },
        )
        ctx = _ctx(tenant_id=tenant_id)
        result = await correct_session(
            session,
            ctx,
            session_id=sid,
            correction_reason="fix the typo",
            attendance_changes=None,
        )
        assert result.outcome is CompletionDecision.REOPENED
        assert str(result.supersedes_id) == str(completion_id)
        assert result.correction_reason == "fix the typo"
        # The new completion row's INSERT carried the supersedes_id + reason.
        completion_inserts = [
            c for c in session.calls
            if c.op == "execute"
            and "INSERT INTO session_completion" in (c.sql or "")
        ]
        assert len(completion_inserts) == 1
        assert str(completion_id) in str(completion_inserts[0].params.get("sup"))


# ===========================================================================
# add_revision (R08)
# ===========================================================================


class TestAddRevision:
    """``add_revision`` — R08: lifecycle guards + boundary check + event choice."""

    async def test_rejected_when_cancelled(self) -> None:
        """A cancelled session does not accept new revisions (line 117)."""
        from ksma.shared.errors import StateConflictError

        session = FakeAsyncSession()
        tenant_id = uuid4()
        sid = _seed_session(
            session,
            tenant_id=tenant_id,
            lifecycle_status=SessionLifecycleStatus.CANCELLED,
        )
        ctx = _ctx(tenant_id=tenant_id)
        with pytest.raises(StateConflictError):
            await add_revision(
                session,
                ctx,
                session_id=sid,
                starts_at=datetime(2026, 1, 1, 8, 0, tzinfo=timezone.utc),
                ends_at=datetime(2026, 1, 1, 9, 0, tzinfo=timezone.utc),
                timezone_str="UTC",
                change_reason="oops",
            )

    async def test_rejected_when_closed(self) -> None:
        """A closed session does not accept new revisions (line 119)."""
        from ksma.shared.errors import StateConflictError

        session = FakeAsyncSession()
        tenant_id = uuid4()
        sid = _seed_session(
            session,
            tenant_id=tenant_id,
            lifecycle_status=SessionLifecycleStatus.CLOSED,
        )
        ctx = _ctx(tenant_id=tenant_id)
        with pytest.raises(StateConflictError) as exc_info:
            await add_revision(
                session,
                ctx,
                session_id=sid,
                starts_at=datetime(2026, 1, 1, 8, 0, tzinfo=timezone.utc),
                ends_at=datetime(2026, 1, 1, 9, 0, tzinfo=timezone.utc),
                timezone_str="UTC",
                change_reason="oops",
            )
        assert "correct" in str(exc_info.value).lower()

    async def test_ends_at_must_be_strictly_after_starts_at(self) -> None:
        """``ends_at <= starts_at`` is rejected (line 120 ``<= -> <``)."""
        from ksma.shared.errors import ValidationError

        session = FakeAsyncSession()
        tenant_id = uuid4()
        sid = _seed_session(
            session,
            tenant_id=tenant_id,
            lifecycle_status=SessionLifecycleStatus.SCHEDULED,
        )
        ctx = _ctx(tenant_id=tenant_id)
        with pytest.raises(ValidationError):
            await add_revision(
                session,
                ctx,
                session_id=sid,
                starts_at=datetime(2026, 1, 1, 8, 0, tzinfo=timezone.utc),
                ends_at=datetime(2026, 1, 1, 8, 0, tzinfo=timezone.utc),
                timezone_str="UTC",
                change_reason="oops",
            )

    async def test_first_revision_makes_effective_and_lifecycle_scheduled(self) -> None:
        """Default ``make_effective=True`` sets lifecycle SCHEDULED + publishes.

        Pins the default ``make_effective: bool = True`` (line 109) so a
        ``True -> False`` flip on the default still produces a Scheduled
        session with a published revision.
        """
        from ksma.session.domain.events import SessionScheduled

        session = FakeAsyncSession()
        tenant_id = uuid4()
        sid = _seed_session(
            session,
            tenant_id=tenant_id,
            lifecycle_status=SessionLifecycleStatus.PLANNED,
        )
        _seed_revision_no_row(session, tenant_id=tenant_id, session_id=sid)
        ctx = _ctx(tenant_id=tenant_id)
        r = await add_revision(
            session,
            ctx,
            session_id=sid,
            starts_at=datetime(2026, 1, 1, 8, 0, tzinfo=timezone.utc),
            ends_at=datetime(2026, 1, 1, 9, 0, tzinfo=timezone.utc),
            timezone_str="UTC",
            change_reason="initial schedule",
        )
        assert r.published_at is not None
        assert r.revision_no == 1
        # Session transitioned to SCHEDULED.
        assert session.rows("session")[0]["lifecycle_status"] == SessionLifecycleStatus.SCHEDULED.value
        # SessionScheduled was emitted (the first-revision path; not Rescheduled).
        # The event is enqueued via ``outbox_messages`` with ``event_type``
        # as a bind parameter — the SQL body is constant, so we look at params.
        scheduled_calls = [
            c for c in session.calls
            if (c.params or {}).get("event_type") == "SessionScheduled"
        ]
        assert len(scheduled_calls) == 1

    async def test_make_effective_false_skips_publication(self) -> None:
        """``make_effective=False`` leaves the session lifecycle untouched."""
        session = FakeAsyncSession()
        tenant_id = uuid4()
        sid = _seed_session(
            session,
            tenant_id=tenant_id,
            lifecycle_status=SessionLifecycleStatus.PLANNED,
        )
        _seed_revision_no_row(session, tenant_id=tenant_id, session_id=sid)
        ctx = _ctx(tenant_id=tenant_id)
        r = await add_revision(
            session,
            ctx,
            session_id=sid,
            starts_at=datetime(2026, 1, 1, 8, 0, tzinfo=timezone.utc),
            ends_at=datetime(2026, 1, 1, 9, 0, tzinfo=timezone.utc),
            timezone_str="UTC",
            change_reason="draft",
            make_effective=False,
        )
        assert r.published_at is None
        # Lifecycle stayed PLANNED.
        assert session.rows("session")[0]["lifecycle_status"] == SessionLifecycleStatus.PLANNED.value


# ===========================================================================
# cancel_session (BR04)
# ===========================================================================


class TestPostCancelSession:
    """``cancel_session`` — BR04: lifecycle=cancelled, NOT counted as completed."""

    async def test_sets_lifecycle_to_cancelled(self) -> None:
        """A scheduled session transitions to CANCELLED, with a CANCEL reason.

        Pins line 169 (``or -> and``) by exercising the
        ``effective_revision_id is None`` branch: the synthetic uuid4()
        fallback must produce a non-null revision_id.
        """
        session = FakeAsyncSession()
        tenant_id = uuid4()
        sid = _seed_session(
            session,
            tenant_id=tenant_id,
            lifecycle_status=SessionLifecycleStatus.SCHEDULED,
        )
        ctx = _ctx(tenant_id=tenant_id)
        await cancel_session(session, ctx, session_id=sid, reason="school holiday")
        row = session.rows("session")[0]
        assert row["lifecycle_status"] == SessionLifecycleStatus.CANCELLED.value
        # The synthetic revision_id was set, with a "CANCELLED: " reason.
        assert row["effective_revision_id"] is not None
        assert row["reschedule_reason"] is not None
        assert "CANCELLED" in row["reschedule_reason"]

    async def test_does_not_count_as_completed(self) -> None:
        """Cancellation does NOT create a SessionCompletion row.

        The session stays open for accounting (closure_id stays None);
        a closed-session completion is never produced for a cancel.
        """
        session = FakeAsyncSession()
        tenant_id = uuid4()
        sid = _seed_session(
            session,
            tenant_id=tenant_id,
            lifecycle_status=SessionLifecycleStatus.SCHEDULED,
        )
        ctx = _ctx(tenant_id=tenant_id)
        await cancel_session(session, ctx, session_id=sid, reason="x")
        row = session.rows("session")[0]
        assert row["closure_id"] is None
        # No completion INSERT was issued.
        completion_inserts = [
            c for c in session.calls
            if c.op == "execute"
            and "INSERT INTO session_completion" in (c.sql or "")
        ]
        assert completion_inserts == []

    async def test_rejects_already_cancelled(self) -> None:
        """Cancelling an already-cancelled session is rejected."""
        from ksma.shared.errors import StateConflictError

        session = FakeAsyncSession()
        tenant_id = uuid4()
        sid = _seed_session(
            session,
            tenant_id=tenant_id,
            lifecycle_status=SessionLifecycleStatus.CANCELLED,
        )
        ctx = _ctx(tenant_id=tenant_id)
        with pytest.raises(StateConflictError):
            await cancel_session(session, ctx, session_id=sid, reason="x")


# ===========================================================================
# write_journal
# ===========================================================================


class TestWriteJournal:
    """``write_journal`` — requires actual_instructor_id and topic."""

    async def test_empty_topic_rejected(self) -> None:
        """An empty topic is a ValidationError (line 259 ``delete not``)."""
        from ksma.shared.errors import ValidationError

        session = FakeAsyncSession()
        tenant_id = uuid4()
        sid = _seed_session(
            session, tenant_id=tenant_id, lifecycle_status=SessionLifecycleStatus.SCHEDULED
        )
        ctx = _ctx(tenant_id=tenant_id)
        with pytest.raises(ValidationError) as exc_info:
            await write_journal(
                session,
                ctx,
                session_id=sid,
                actual_instructor_id=uuid4(),
                topic="",
            )
        assert "topic" in str(exc_info.value)

    async def test_rejects_cancelled_session(self) -> None:
        """A cancelled session does not accept a teaching journal."""
        from ksma.shared.errors import StateConflictError

        session = FakeAsyncSession()
        tenant_id = uuid4()
        sid = _seed_session(
            session, tenant_id=tenant_id, lifecycle_status=SessionLifecycleStatus.CANCELLED
        )
        ctx = _ctx(tenant_id=tenant_id)
        with pytest.raises(StateConflictError):
            await write_journal(
                session,
                ctx,
                session_id=sid,
                actual_instructor_id=uuid4(),
                topic="Algebra",
            )

    async def test_persists_topic_and_actual_instructor(self) -> None:
        """The journal row is upserted with topic + actual_instructor_id."""
        session = FakeAsyncSession()
        tenant_id = uuid4()
        sid = _seed_session(
            session, tenant_id=tenant_id, lifecycle_status=SessionLifecycleStatus.SCHEDULED
        )
        instructor_id = uuid4()
        ctx = _ctx(tenant_id=tenant_id)
        j = await write_journal(
            session,
            ctx,
            session_id=sid,
            actual_instructor_id=instructor_id,
            topic="Algebra",
            activities="problem sets",
        )
        assert j.topic == "Algebra"
        assert j.actual_instructor_id == instructor_id
        assert j.activities == "problem sets"
        # The INSERT bind params carried topic + instructor_id + session_id.
        inserts = [
            c for c in session.calls
            if c.op == "execute" and "INSERT INTO teaching_journal" in (c.sql or "")
        ]
        assert len(inserts) == 1
        assert inserts[0].params.get("topic") == "Algebra"
        assert inserts[0].params.get("mid") == str(instructor_id)


# ===========================================================================
# correct_attendance — empty correction_reason rejected (line 225)
# ===========================================================================


class TestCorrectAttendance:
    """``correct_attendance`` — V09: empty correction_reason is rejected."""

    async def test_empty_correction_reason_rejected(self) -> None:
        """Empty ``correction_reason`` is a ValidationError (line 225 ``delete not``)."""
        from ksma.shared.errors import ValidationError

        session = FakeAsyncSession()
        tenant_id = uuid4()
        ctx = _ctx(tenant_id=tenant_id)
        with pytest.raises(ValidationError) as exc_info:
            await correct_attendance(
                session,
                ctx,
                session_id=uuid4(),
                enrollment_id=uuid4(),
                attendance_code=None,
                correction_reason="",
            )
        assert "correction_reason" in str(exc_info.value)
        # No UPDATE against attendance was issued.
        updates = [c for c in session.calls if c.op == "execute" and "UPDATE attendance" in (c.sql or "")]
        assert updates == []

    async def test_valid_correction_proceeds(self) -> None:
        """A non-empty correction_reason reaches the UPDATE path."""
        from ksma.session.domain import AttendanceCode

        session = FakeAsyncSession()
        tenant_id = uuid4()
        ctx = _ctx(tenant_id=tenant_id)
        sid = uuid4()
        eid = uuid4()
        # correct_attendance finishes by calling self.get(...) which expects
        # a row to be findable. Seed the row so the return value is real.
        session.seed(
            "attendance",
            {
                "id": str(uuid4()),
                "tenant_id": str(tenant_id),
                "session_id": str(sid),
                "enrollment_id": str(eid),
                "attendance_code": "izin",
                "note": None,
                "recorded_at": None,
                "recorded_by": "tester",
                "correction_reason": "fix",
                "created_at": None,
                "created_by": "tester",
                "updated_at": None,
                "updated_by": "tester",
            },
        )
        result = await correct_attendance(
            session,
            ctx,
            session_id=sid,
            enrollment_id=eid,
            attendance_code=AttendanceCode.IZIN,
            correction_reason="valid reason",
        )
        assert result.attendance_code is AttendanceCode.IZIN
        # The fake mutates the seeded row in place via _update(), which
        # means the stored correction_reason now matches what was bound.
        assert result.correction_reason == "valid reason"
        # The UPDATE carry the non-empty correction_reason.
        updates = [c for c in session.calls if c.op == "execute" and "UPDATE attendance" in (c.sql or "")]
        assert len(updates) == 1
        assert updates[0].params.get("cr") == "valid reason"