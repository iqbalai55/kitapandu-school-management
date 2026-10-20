"""Unit tests for ``ksma.session.infrastructure.repositories`` — the JSONB
mapping adapters + the ``next_*_no`` arithmetic + ``list_for_session``.

Seam
----
``ksma.session.infrastructure.repositories``. Public surface pinned here:

- ``_to_session``, ``_to_revision``, ``_to_journal``, ``_to_completion``:
  the JSONB-read adapters. Each takes a row-like and produces the matching
  domain entity — pinning the field-by-field round-trip so an attribute
  removal or column swap is caught.
- ``SessionRepository.next_session_no``, ``SessionRevisionRepository
  .next_revision_no``, ``SessionCompletionRepository.next_completion_no``:
  the ``(MAX(n) OR 0) + 1`` arithmetic. The four surviving mutants on each
  line (``or -> and`` / ``0 -> 1`` / ``+ -> -`` / ``1 -> 0``) all collapse
  to a wrong answer on the ``MAX(n) > 0`` case; the empty-table case
  catches ``0 -> 1`` once the ``or`` is gone.
- ``AttendanceRepository.list_for_session``: ordering is the
  ``ORDER BY created_at`` literal in the SQL body — pinned so a future
  reorder is caught.
- ``AttendanceRepository.correct`` (line 315 ``delete not``): empty
  ``correction_reason`` is a ``ValidationError``.

Persistence is the in-memory ``FakeAsyncSession`` toolkit from
``tests/unit/conftest_helpers.py``. The ``_to_*`` adapters are tested
directly with ``FakeRow`` — no SQL round-trip needed.

What is NOT asserted here (lives in the integration tier):

- The real Postgres ``COALESCE(MAX(...), 0) AS n`` semantics — the fake
  reads the ``n`` alias from a seeded row.
- The Postgres ``ON CONFLICT DO UPDATE`` upsert (the unit fake simply
  appends).
"""
from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import pytest

from ksma.session.domain import (
    Attendance,
    AttendanceCode,
    CompletionDecision,
    SessionLifecycleStatus,
)
from ksma.session.infrastructure.repositories import (
    AttendanceRepository,
    SessionCompletionRepository,
    SessionRepository,
    SessionRevisionRepository,
    _to_attendance,
    _to_completion,
    _to_journal,
    _to_revision,
    _to_session,
)
from tests.unit.conftest_helpers import FakeAsyncSession, FakeRow

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _session_row(
    *,
    session_id: UUID | None = None,
    tenant_id: UUID | None = None,
    unit_id: UUID | None = None,
    batch_id: UUID | None = None,
    session_no: int = 1,
    lifecycle_status: SessionLifecycleStatus = SessionLifecycleStatus.PLANNED,
    closure_id: UUID | None = None,
    effective_revision_id: UUID | None = None,
) -> FakeRow:
    sid = session_id or uuid4()
    tid = tenant_id or uuid4()
    uid = unit_id or uuid4()
    bid = batch_id or uuid4()
    return FakeRow(
        {
            "id": str(sid),
            "tenant_id": str(tid),
            "unit_id": str(uid),
            "batch_id": str(bid),
            "session_no": session_no,
            "effective_revision_id": str(effective_revision_id) if effective_revision_id else None,
            "lifecycle_status": lifecycle_status.value,
            "closure_id": str(closure_id) if closure_id else None,
            "reschedule_reason": None,
            "created_at": None,
            "created_by": "tester",
            "updated_at": None,
            "updated_by": "tester",
        }
    )


def _revision_row(
    *,
    revision_id: UUID | None = None,
    session_id: UUID | None = None,
    revision_no: int = 1,
    starts_at: Any = None,
    ends_at: Any = None,
    location: Any = None,
) -> FakeRow:
    rid = revision_id or uuid4()
    return FakeRow(
        {
            "id": str(rid),
            "tenant_id": str(uuid4()),
            "session_id": str(session_id or uuid4()),
            "revision_no": revision_no,
            "starts_at": starts_at,
            "ends_at": ends_at,
            "timezone": "Asia/Jakarta",
            "location": location,
            "change_reason": "initial",
            "published_at": None,
            "created_at": None,
            "created_by": "tester",
            "updated_at": None,
            "updated_by": "tester",
        }
    )


def _journal_row(
    *,
    session_id: UUID | None = None,
    topic: str = "Algebra",
    actual_instructor_id: UUID | None = None,
) -> FakeRow:
    return FakeRow(
        {
            "id": str(uuid4()),
            "tenant_id": str(uuid4()),
            "session_id": str(session_id or uuid4()),
            "actual_instructor_id": str(actual_instructor_id or uuid4()),
            "topic": topic,
            "activities": None,
            "internal_note": None,
            "portal_summary": None,
            "follow_up": None,
            "created_at": None,
            "created_by": "tester",
            "updated_at": None,
            "updated_by": "tester",
        }
    )


def _completion_row(
    *,
    completion_id: UUID | None = None,
    session_id: UUID | None = None,
    completion_no: int = 1,
    supersedes_id: UUID | None = None,
    outcome: CompletionDecision = CompletionDecision.COMPLETED,
    correction_reason: str | None = None,
) -> FakeRow:
    return FakeRow(
        {
            "id": str(completion_id or uuid4()),
            "tenant_id": str(uuid4()),
            "session_id": str(session_id or uuid4()),
            "completion_no": completion_no,
            "decision": {"actor": "tester"},
            "checklist_snapshot": {"rule_version": "R09-v1"},
            "policy_id": None,
            "supersedes_id": str(supersedes_id) if supersedes_id else None,
            "correction_reason": correction_reason,
            "outcome": outcome.value,
            "created_at": None,
            "created_by": "tester",
            "updated_at": None,
            "updated_by": "tester",
        }
    )


# ===========================================================================
# JSONB adapters — _to_session / _to_revision / _to_journal / _to_completion
# ===========================================================================


class TestJsonbAdapters:
    """The ``_to_*`` row-to-entity adapters: field-by-field round-trips."""

    def test_to_session_carries_every_field(self) -> None:
        """Every ``_to_session`` column lands on the Session entity."""
        sid = uuid4()
        tid = uuid4()
        uid = uuid4()
        bid = uuid4()
        rid = uuid4()
        cid = uuid4()
        s = _to_session(
            _session_row(
                session_id=sid,
                tenant_id=tid,
                unit_id=uid,
                batch_id=bid,
                session_no=7,
                lifecycle_status=SessionLifecycleStatus.SCHEDULED,
                closure_id=cid,
                effective_revision_id=rid,
            )
        )
        assert str(s.id) == str(sid)
        assert str(s.tenant_id) == str(tid)
        assert str(s.unit_id) == str(uid)
        assert str(s.batch_id) == str(bid)
        assert s.session_no == 7
        assert s.lifecycle_status is SessionLifecycleStatus.SCHEDULED
        assert str(s.closure_id) == str(cid)
        assert str(s.effective_revision_id) == str(rid)
        assert s.created_by == "tester"
        assert s.updated_by == "tester"

    def test_to_revision_carries_every_field(self) -> None:
        """Every ``_to_revision`` column lands on the SessionRevision entity."""
        rid = uuid4()
        sid = uuid4()
        starts = "2026-01-01T08:00:00+00:00"
        ends = "2026-01-01T09:00:00+00:00"
        location = {"room": "R-101", "online_url": None}
        r = _to_revision(
            _revision_row(
                revision_id=rid,
                session_id=sid,
                revision_no=3,
                starts_at=starts,
                ends_at=ends,
                location=location,
            )
        )
        assert str(r.id) == str(rid)
        assert str(r.session_id) == str(sid)
        assert r.revision_no == 3
        assert r.starts_at == starts
        assert r.ends_at == ends
        assert r.timezone == "Asia/Jakarta"
        assert r.location == location
        assert r.change_reason == "initial"

    def test_to_journal_carries_topic_and_instructor(self) -> None:
        """Every ``_to_journal`` column lands on the TeachingJournal entity."""
        sid = uuid4()
        instructor = uuid4()
        j = _to_journal(
            _journal_row(session_id=sid, topic="Algebra", actual_instructor_id=instructor)
        )
        assert str(j.session_id) == str(sid)
        assert str(j.actual_instructor_id) == str(instructor)
        assert j.topic == "Algebra"
        assert j.activities is None
        assert j.portal_summary is None

    def test_to_completion_carries_supersedes_and_outcome(self) -> None:
        """Every ``_to_completion`` column lands on the SessionCompletion."""
        cid = uuid4()
        sid = uuid4()
        supersedes = uuid4()
        c = _to_completion(
            _completion_row(
                completion_id=cid,
                session_id=sid,
                completion_no=2,
                supersedes_id=supersedes,
                outcome=CompletionDecision.REOPENED,
                correction_reason="fix the typo",
            )
        )
        assert str(c.id) == str(cid)
        assert str(c.session_id) == str(sid)
        assert c.completion_no == 2
        assert c.outcome is CompletionDecision.REOPENED
        assert str(c.supersedes_id) == str(supersedes)
        assert c.correction_reason == "fix the typo"
        # The JSONB-typed decision + checklist dicts survive as-is.
        assert c.decision == {"actor": "tester"}
        assert c.checklist_snapshot == {"rule_version": "R09-v1"}

    def test_to_attendance_preserves_code_and_correction_reason(self) -> None:
        """``_to_attendance`` round-trips the Attendance entity end-to-end."""
        row = FakeRow(
            {
                "id": str(uuid4()),
                "tenant_id": str(uuid4()),
                "session_id": str(uuid4()),
                "enrollment_id": str(uuid4()),
                "attendance_code": "izin",
                "note": "family matter",
                "recorded_at": None,
                "recorded_by": "tester",
                "correction_reason": "late arrival",
                "created_at": None,
                "created_by": "tester",
                "updated_at": None,
                "updated_by": "tester",
            }
        )
        a = _to_attendance(row)
        assert isinstance(a, Attendance)
        assert a.attendance_code is AttendanceCode.IZIN
        assert a.correction_reason == "late arrival"
        assert a.note == "family matter"


# ===========================================================================
# next_*_no arithmetic — `(MAX(n) OR 0) + 1`
# ===========================================================================


class TestNextNoArithmetic:
    """The ``next_*_no`` methods — ``(MAX(n) OR 0) + 1`` correctness.

    Pins the four surviving mutants on the shared arithmetic pattern:
    ``or -> and`` / ``0 -> 1`` / ``+ -> -`` / ``1 -> 0``. The empty-table
    case pins the ``0 + 1 = 1`` answer; the seeded case pins the
    ``MAX(n) + 1`` answer.
    """

    async def test_next_session_no_empty_returns_one(self) -> None:
        """Empty table: no rows -> ``(0 OR 0) + 1`` = ``1``.

        Catches ``0 -> 1`` (would yield ``(0 OR 1) + 1 = 2``).
        """
        session = FakeAsyncSession()
        tenant_id = uuid4()
        batch_id = uuid4()
        repo = SessionRepository(session, tenant_id)
        session.seed(
            "session",
            {"n": 0, "batch_id": str(batch_id), "tenant_id": str(tenant_id)},
        )
        # AsyncMock spy on execute(): the repository must consult the DB
        # (vs caching/short-circuiting), so the spy gets called exactly
        # once for the MAX query.
        spy = AsyncMock(wraps=session.execute)
        session.execute = spy  # type: ignore[method-assign]
        assert await repo.next_session_no(batch_id) == 1
        # And the spy recorded the COALESCE MAX query.
        max_calls = [
            c for c in session.calls
            if c.op == "execute" and "COALESCE(MAX(session_no)" in (c.sql or "")
        ]
        assert len(max_calls) == 1

    async def test_next_session_no_increments_max(self) -> None:
        """Seeded ``n=5`` -> ``(5 OR 0) + 1`` = ``6``.

        Catches ``or -> and`` (-> 1), ``+ -> -`` (-> 4), ``1 -> 0`` (-> 5).
        """
        session = FakeAsyncSession()
        tenant_id = uuid4()
        batch_id = uuid4()
        repo = SessionRepository(session, tenant_id)
        session.seed(
            "session",
            {"n": 5, "batch_id": str(batch_id), "tenant_id": str(tenant_id)},
        )
        assert await repo.next_session_no(batch_id) == 6

    async def test_next_revision_no_empty_returns_one(self) -> None:
        """Empty revision table -> ``1``."""
        session = FakeAsyncSession()
        tenant_id = uuid4()
        session_id = uuid4()
        repo = SessionRevisionRepository(session, tenant_id)
        session.seed(
            "session_revision",
            {"n": 0, "session_id": str(session_id), "tenant_id": str(tenant_id)},
        )
        assert await repo.next_revision_no(session_id) == 1

    async def test_next_revision_no_increments_max(self) -> None:
        """Seeded ``n=5`` -> ``6``."""
        session = FakeAsyncSession()
        tenant_id = uuid4()
        session_id = uuid4()
        repo = SessionRevisionRepository(session, tenant_id)
        session.seed(
            "session_revision",
            {"n": 5, "session_id": str(session_id), "tenant_id": str(tenant_id)},
        )
        assert await repo.next_revision_no(session_id) == 6

    async def test_next_completion_no_empty_returns_one(self) -> None:
        """Empty completion table -> ``1``."""
        session = FakeAsyncSession()
        tenant_id = uuid4()
        session_id = uuid4()
        repo = SessionCompletionRepository(session, tenant_id)
        session.seed(
            "session_completion",
            {"n": 0, "session_id": str(session_id), "tenant_id": str(tenant_id)},
        )
        assert await repo.next_completion_no(session_id) == 1

    async def test_next_completion_no_increments_max(self) -> None:
        """Seeded ``n=5`` -> ``6``."""
        session = FakeAsyncSession()
        tenant_id = uuid4()
        session_id = uuid4()
        repo = SessionCompletionRepository(session, tenant_id)
        session.seed(
            "session_completion",
            {"n": 5, "session_id": str(session_id), "tenant_id": str(tenant_id)},
        )
        assert await repo.next_completion_no(session_id) == 6


# ===========================================================================
# AttendanceRepository.correct — empty correction_reason is rejected
# ===========================================================================


class TestAttendanceCorrect:
    """``AttendanceRepository.correct`` — empty correction_reason is rejected."""

    async def test_empty_correction_reason_rejected(self) -> None:
        """Empty ``correction_reason`` raises ``ValidationError`` (line 315).

        Pins the ``if not correction_reason`` guard so a ``delete not``
        flip lets an empty-reason UPDATE through to the attendance row.
        """
        from ksma.shared.errors import ValidationError  # noqa: PLC0415

        session = FakeAsyncSession()
        tenant_id = uuid4()
        repo = AttendanceRepository(session, tenant_id)
        with pytest.raises(ValidationError) as exc_info:
            await repo.correct(
                session_id=uuid4(),
                enrollment_id=uuid4(),
                attendance_code=AttendanceCode.IZIN,
                correction_reason="",
                recorded_by="tester",
                recorded_at=None,
            )
        assert "correction_reason" in str(exc_info.value)
        # No UPDATE was issued.
        updates = [c for c in session.calls if c.op == "execute" and "UPDATE attendance" in (c.sql or "")]
        assert updates == []

    async def test_valid_correction_issues_update(self) -> None:
        """A non-empty ``correction_reason`` proceeds to the UPDATE."""
        session = FakeAsyncSession()
        tenant_id = uuid4()
        repo = AttendanceRepository(session, tenant_id)
        sid = uuid4()
        eid = uuid4()
        # The .correct() method calls self.get(...) afterwards, which expects
        # an attendance row to be findable in the fake table. Seed one.
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
        result = await repo.correct(
            session_id=sid,
            enrollment_id=eid,
            attendance_code=AttendanceCode.IZIN,
            correction_reason="correction note",
            recorded_by="tester",
            recorded_at=None,
        )
        assert isinstance(result, Attendance)
        assert result.attendance_code is AttendanceCode.IZIN
        # The UPDATE carry the non-empty correction_reason.
        updates = [c for c in session.calls if c.op == "execute" and "UPDATE attendance" in (c.sql or "")]
        assert len(updates) == 1
        assert updates[0].params.get("cr") == "correction note"


# ===========================================================================
# AttendanceRepository.list_for_session — ordering
# ===========================================================================


class TestListForSession:
    """``AttendanceRepository.list_for_session`` — ordering + filter shape."""

    async def test_returns_only_rows_for_session(self) -> None:
        """The query is bounded by ``session_id`` AND ``tenant_id``."""
        session = FakeAsyncSession()
        tenant_id = uuid4()
        sid = uuid4()
        other_sid = uuid4()
        repo = AttendanceRepository(session, tenant_id)
        for _ in range(2):
            session.seed(
                "attendance",
                {
                    "id": str(uuid4()),
                    "tenant_id": str(tenant_id),
                    "session_id": str(sid),
                    "enrollment_id": str(uuid4()),
                    "attendance_code": "hadir",
                    "note": None,
                    "recorded_at": None,
                    "recorded_by": "x",
                    "correction_reason": None,
                    "created_at": "2026-01-01",
                    "created_by": "x",
                    "updated_at": None,
                    "updated_by": "x",
                },
            )
        # An unrelated session's row must NOT come back.
        session.seed(
            "attendance",
            {
                "id": str(uuid4()),
                "tenant_id": str(tenant_id),
                "session_id": str(other_sid),
                "enrollment_id": str(uuid4()),
                "attendance_code": "hadir",
                "note": None,
                "recorded_at": None,
                "recorded_by": "x",
                "correction_reason": None,
                "created_at": "2026-01-01",
                "created_by": "x",
                "updated_at": None,
                "updated_by": "x",
            },
        )
        rows = await repo.list_for_session(sid)
        assert len(rows) == 2
        assert all(str(r.session_id) == str(sid) for r in rows)

    async def test_sql_orders_by_created_at(self) -> None:
        """The SQL body pins ``ORDER BY created_at`` for the listing.

        The fake does not actually sort, but the literal in the SQL is the
        contract — a future change to ``ORDER BY id`` (or no ORDER BY)
        must surface here. Catches an `ORDER BY` literal removal.
        """
        session = FakeAsyncSession()
        tenant_id = uuid4()
        sid = uuid4()
        repo = AttendanceRepository(session, tenant_id)
        # Seed one row so the fake's WHERE has a row to operate on.
        session.seed(
            "attendance",
            {
                "id": str(uuid4()),
                "tenant_id": str(tenant_id),
                "session_id": str(sid),
                "enrollment_id": str(uuid4()),
                "attendance_code": "hadir",
                "note": None,
                "recorded_at": None,
                "recorded_by": "x",
                "correction_reason": None,
                "created_at": "2026-01-01",
                "created_by": "x",
                "updated_at": None,
                "updated_by": "x",
            },
        )
        await repo.list_for_session(sid)
        # The recorded call's SQL must carry the ordering literal.
        list_calls = [
            c for c in session.calls
            if c.op == "execute" and "FROM attendance" in (c.sql or "")
            and "ORDER BY created_at" in (c.sql or "")
        ]
        assert len(list_calls) == 1