"""Session repositories — tenant-scoped via RLS + explicit filter."""
from __future__ import annotations

from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from ksma.session.domain import (
    Attendance,
    AttendanceCode,
    CompletionDecision,
    Session,
    SessionCompletion,
    SessionLifecycleStatus,
    SessionRevision,
    TeachingJournal,
)


def _json(d: Any) -> str:
    import json

    return json.dumps(d, default=str)


# ---------------------------------------------------------------------------
# Session + SessionRevision
# ---------------------------------------------------------------------------


class SessionRepository:
    def __init__(self, session: AsyncSession, tenant_id: UUID) -> None:
        self.session = session
        self.tenant_id = tenant_id

    async def create(self, s: Session) -> None:
        await self.session.execute(
            text(
                """
                INSERT INTO session
                  (id, tenant_id, unit_id, batch_id, session_no,
                   effective_revision_id, lifecycle_status, closure_id,
                   reschedule_reason, created_at, created_by,
                   updated_at, updated_by)
                VALUES
                  (:id, :tid, :uid, :bid, :no, NULL, :status, NULL, NULL,
                   now(), :cb, now(), :ub)
                """
            ),
            {
                "id": str(s.id),
                "tid": str(self.tenant_id),
                "uid": str(s.unit_id),
                "bid": str(s.batch_id),
                "no": s.session_no,
                "status": s.lifecycle_status.value,
                "cb": s.created_by,
                "ub": s.updated_by,
            },
        )

    async def get(self, id: UUID) -> Session | None:
        row = (
            await self.session.execute(
                text(
                    """
                    SELECT id, tenant_id, unit_id, batch_id, session_no,
                           effective_revision_id, lifecycle_status, closure_id,
                           reschedule_reason, created_at, created_by,
                           updated_at, updated_by
                    FROM session
                    WHERE id = :id AND tenant_id = :tid
                    """
                ),
                {"id": str(id), "tid": str(self.tenant_id)},
            )
        ).first()
        return _to_session(row) if row else None

    async def lock_for_update(self, id: UUID) -> Session | None:
        row = (
            await self.session.execute(
                text(
                    """
                    SELECT id, tenant_id, unit_id, batch_id, session_no,
                           effective_revision_id, lifecycle_status, closure_id,
                           reschedule_reason, created_at, created_by,
                           updated_at, updated_by
                    FROM session
                    WHERE id = :id AND tenant_id = :tid
                    FOR UPDATE
                    """
                ),
                {"id": str(id), "tid": str(self.tenant_id)},
            )
        ).first()
        return _to_session(row) if row else None

    async def set_effective_revision(
        self, id: UUID, revision_id: UUID, reschedule_reason: str | None, actor: str
    ) -> None:
        await self.session.execute(
            text(
                "UPDATE session SET effective_revision_id=:r, "
                "reschedule_reason=:rr, updated_at=now(), updated_by=:u "
                "WHERE id=:id AND tenant_id=:tid"
            ),
            {
                "id": str(id),
                "tid": str(self.tenant_id),
                "r": str(revision_id),
                "rr": reschedule_reason,
                "u": actor,
            },
        )

    async def set_lifecycle(
        self, id: UUID, status: SessionLifecycleStatus, actor: str
    ) -> None:
        await self.session.execute(
            text(
                "UPDATE session SET lifecycle_status=:s, updated_at=now(), "
                "updated_by=:u WHERE id=:id AND tenant_id=:tid"
            ),
            {"id": str(id), "tid": str(self.tenant_id), "s": status.value, "u": actor},
        )

    async def set_closure(self, id: UUID, closure_id: UUID, actor: str) -> None:
        await self.session.execute(
            text(
                "UPDATE session SET closure_id=:c, updated_at=now(), updated_by=:u "
                "WHERE id=:id AND tenant_id=:tid"
            ),
            {"id": str(id), "tid": str(self.tenant_id), "c": str(closure_id), "u": actor},
        )

    async def next_session_no(self, batch_id: UUID) -> int:
        row = (
            await self.session.execute(
                text(
                    "SELECT COALESCE(MAX(session_no), 0) AS n FROM session "
                    "WHERE batch_id = :bid AND tenant_id = :tid"
                ),
                {"bid": str(batch_id), "tid": str(self.tenant_id)},
            )
        ).first()
        return (row.n or 0) + 1


class SessionRevisionRepository:
    def __init__(self, session: AsyncSession, tenant_id: UUID) -> None:
        self.session = session
        self.tenant_id = tenant_id

    async def create(self, r: SessionRevision) -> None:
        await self.session.execute(
            text(
                """
                INSERT INTO session_revision
                  (id, tenant_id, session_id, revision_no, starts_at, ends_at,
                   timezone, location, change_reason, published_at,
                   created_at, created_by, updated_at, updated_by)
                VALUES
                  (:id, :tid, :sid, :rn, :sa, :ea, :tz,
                   CAST(:loc AS JSONB), :cr, :pa,
                   now(), :cb, now(), :ub)
                """
            ),
            {
                "id": str(r.id),
                "tid": str(self.tenant_id),
                "sid": str(r.session_id),
                "rn": r.revision_no,
                "sa": r.starts_at,
                "ea": r.ends_at,
                "tz": r.timezone,
                "loc": _json(r.location) if r.location else None,
                "cr": r.change_reason,
                "pa": r.published_at,
                "cb": r.created_by,
                "ub": r.updated_by,
            },
        )

    async def get(self, id: UUID) -> SessionRevision | None:
        row = (
            await self.session.execute(
                text(
                    """
                    SELECT id, tenant_id, session_id, revision_no, starts_at, ends_at,
                           timezone, location, change_reason, published_at,
                           created_at, created_by, updated_at, updated_by
                    FROM session_revision WHERE id = :id AND tenant_id = :tid
                    """
                ),
                {"id": str(id), "tid": str(self.tenant_id)},
            )
        ).first()
        return _to_revision(row) if row else None

    async def next_revision_no(self, session_id: UUID) -> int:
        row = (
            await self.session.execute(
                text(
                    "SELECT COALESCE(MAX(revision_no), 0) AS n FROM session_revision "
                    "WHERE session_id = :sid AND tenant_id = :tid"
                ),
                {"sid": str(session_id), "tid": str(self.tenant_id)},
            )
        ).first()
        return (row.n or 0) + 1


# ---------------------------------------------------------------------------
# Attendance
# ---------------------------------------------------------------------------


class AttendanceRepository:
    def __init__(self, session: AsyncSession, tenant_id: UUID) -> None:
        self.session = session
        self.tenant_id = tenant_id

    async def upsert(
        self,
        *,
        session_id: UUID,
        enrollment_id: UUID,
        attendance_code: AttendanceCode | None,
        note: str | None,
        recorded_by: str,
        recorded_at: Any,
    ) -> Attendance:
        await self.session.execute(
            text(
                """
                INSERT INTO attendance
                  (id, tenant_id, session_id, enrollment_id, attendance_code, note,
                   recorded_at, recorded_by, correction_reason,
                   created_at, created_by, updated_at, updated_by)
                VALUES
                  (ksma_uuid(), :tid, :sid, :eid, :ac, :note, :ra, :rb, NULL,
                   now(), :rb2, now(), :rb2)
                ON CONFLICT (session_id, enrollment_id) DO UPDATE
                  SET attendance_code = EXCLUDED.attendance_code,
                      note             = EXCLUDED.note,
                      recorded_at      = EXCLUDED.recorded_at,
                      recorded_by      = EXCLUDED.recorded_by,
                      correction_reason = NULL,
                      updated_at       = now(),
                      updated_by       = EXCLUDED.recorded_by
                """
            ),
            {
                "tid": str(self.tenant_id),
                "sid": str(session_id),
                "eid": str(enrollment_id),
                "ac": attendance_code.value if attendance_code else None,
                "note": note,
                "ra": recorded_at,
                "rb": recorded_by,
                "rb2": recorded_by,
            },
        )
        return await self.get(session_id=session_id, enrollment_id=enrollment_id)  # type: ignore[return-value]

    async def get(self, *, session_id: UUID, enrollment_id: UUID) -> Attendance | None:
        row = (
            await self.session.execute(
                text(
                    """
                    SELECT id, tenant_id, session_id, enrollment_id, attendance_code, note,
                           recorded_at, recorded_by, correction_reason,
                           created_at, created_by, updated_at, updated_by
                    FROM attendance
                    WHERE session_id = :sid AND enrollment_id = :eid
                      AND tenant_id = :tid
                    """
                ),
                {"sid": str(session_id), "eid": str(enrollment_id), "tid": str(self.tenant_id)},
            )
        ).first()
        return _to_attendance(row) if row else None

    async def list_for_session(self, session_id: UUID) -> list[Attendance]:
        rows = (
            await self.session.execute(
                text(
                    """
                    SELECT id, tenant_id, session_id, enrollment_id, attendance_code, note,
                           recorded_at, recorded_by, correction_reason,
                           created_at, created_by, updated_at, created_by AS updated_by
                    FROM attendance
                    WHERE session_id = :sid AND tenant_id = :tid
                    ORDER BY created_at
                    """
                ),
                {"sid": str(session_id), "tid": str(self.tenant_id)},
            )
        ).all()
        return [_to_attendance(r) for r in rows]

    async def correct(
        self,
        *,
        session_id: UUID,
        enrollment_id: UUID,
        attendance_code: AttendanceCode | None,
        correction_reason: str,
        recorded_by: str,
        recorded_at: Any,
    ) -> Attendance:
        if not correction_reason:
            from ksma.shared.errors import ValidationError

            raise ValidationError("correction_reason is required for closed-session corrections")
        await self.session.execute(
            text(
                """
                UPDATE attendance
                SET attendance_code = :ac,
                    correction_reason = :cr,
                    recorded_at = :ra,
                    recorded_by = :rb,
                    updated_at = now(),
                    updated_by = :rb
                WHERE session_id = :sid AND enrollment_id = :eid
                  AND tenant_id = :tid
                """
            ),
            {
                "ac": attendance_code.value if attendance_code else None,
                "cr": correction_reason,
                "ra": recorded_at,
                "rb": recorded_by,
                "sid": str(session_id),
                "eid": str(enrollment_id),
                "tid": str(self.tenant_id),
            },
        )
        return await self.get(session_id=session_id, enrollment_id=enrollment_id)  # type: ignore[return-value]


# ---------------------------------------------------------------------------
# TeachingJournal
# ---------------------------------------------------------------------------


class TeachingJournalRepository:
    def __init__(self, session: AsyncSession, tenant_id: UUID) -> None:
        self.session = session
        self.tenant_id = tenant_id

    async def upsert(self, j: TeachingJournal) -> None:
        await self.session.execute(
            text(
                """
                INSERT INTO teaching_journal
                  (id, tenant_id, session_id, actual_instructor_id, topic, activities,
                   internal_note, portal_summary, follow_up,
                   created_at, created_by, updated_at, updated_by)
                VALUES
                  (:id, :tid, :sid, :mid, :topic, :act, :inn, :ps, :fu,
                   now(), :cb, now(), :ub)
                ON CONFLICT (session_id) DO UPDATE
                  SET topic = EXCLUDED.topic,
                      activities = EXCLUDED.activities,
                      internal_note = EXCLUDED.internal_note,
                      portal_summary = EXCLUDED.portal_summary,
                      follow_up = EXCLUDED.follow_up,
                      actual_instructor_id = EXCLUDED.actual_instructor_id,
                      updated_at = now(),
                      updated_by = EXCLUDED.created_by
                """
            ),
            {
                "id": str(j.id),
                "tid": str(self.tenant_id),
                "sid": str(j.session_id),
                "mid": str(j.actual_instructor_id),
                "topic": j.topic,
                "act": j.activities,
                "inn": j.internal_note,
                "ps": j.portal_summary,
                "fu": j.follow_up,
                "cb": j.created_by,
                "ub": j.updated_by,
            },
        )

    async def get(self, session_id: UUID) -> TeachingJournal | None:
        row = (
            await self.session.execute(
                text(
                    """
                    SELECT id, tenant_id, session_id, actual_instructor_id, topic, activities,
                           internal_note, portal_summary, follow_up,
                           created_at, created_by, updated_at, updated_by
                    FROM teaching_journal
                    WHERE session_id = :sid AND tenant_id = :tid
                    """
                ),
                {"sid": str(session_id), "tid": str(self.tenant_id)},
            )
        ).first()
        return _to_journal(row) if row else None


# ---------------------------------------------------------------------------
# SessionCompletion
# ---------------------------------------------------------------------------


class SessionCompletionRepository:
    def __init__(self, session: AsyncSession, tenant_id: UUID) -> None:
        self.session = session
        self.tenant_id = tenant_id

    async def create(self, c: SessionCompletion) -> None:
        await self.session.execute(
            text(
                """
                INSERT INTO session_completion
                  (id, tenant_id, session_id, completion_no, decision, checklist_snapshot,
                   policy_id, supersedes_id, correction_reason, outcome,
                   created_at, created_by, updated_at, updated_by)
                VALUES
                  (:id, :tid, :sid, :no, CAST(:dec AS JSONB), CAST(:ck AS JSONB),
                   :pol, :sup, :cr, :outcome,
                   now(), :cb, now(), :ub)
                """
            ),
            {
                "id": str(c.id),
                "tid": str(self.tenant_id),
                "sid": str(c.session_id),
                "no": c.completion_no,
                "dec": _json(c.decision),
                "ck": _json(c.checklist_snapshot),
                "pol": str(c.policy_id) if c.policy_id else None,
                "sup": str(c.supersedes_id) if c.supersedes_id else None,
                "cr": c.correction_reason,
                "outcome": c.outcome.value,
                "cb": c.created_by,
                "ub": c.updated_by,
            },
        )

    async def latest_for_session(self, session_id: UUID) -> SessionCompletion | None:
        row = (
            await self.session.execute(
                text(
                    """
                    SELECT id, tenant_id, session_id, completion_no, decision, checklist_snapshot,
                           policy_id, supersedes_id, correction_reason, outcome,
                           created_at, created_by, updated_at, updated_by
                    FROM session_completion
                    WHERE session_id = :sid AND tenant_id = :tid
                    ORDER BY completion_no DESC
                    LIMIT 1
                    """
                ),
                {"sid": str(session_id), "tid": str(self.tenant_id)},
            )
        ).first()
        return _to_completion(row) if row else None

    async def get(self, id: UUID) -> SessionCompletion | None:
        row = (
            await self.session.execute(
                text(
                    """
                    SELECT id, tenant_id, session_id, completion_no, decision, checklist_snapshot,
                           policy_id, supersedes_id, correction_reason, outcome,
                           created_at, created_by, updated_at, updated_by
                    FROM session_completion
                    WHERE id = :id AND tenant_id = :tid
                    """
                ),
                {"id": str(id), "tid": str(self.tenant_id)},
            )
        ).first()
        return _to_completion(row) if row else None

    async def next_completion_no(self, session_id: UUID) -> int:
        row = (
            await self.session.execute(
                text(
                    "SELECT COALESCE(MAX(completion_no), 0) AS n FROM session_completion "
                    "WHERE session_id = :sid AND tenant_id = :tid"
                ),
                {"sid": str(session_id), "tid": str(self.tenant_id)},
            )
        ).first()
        return (row.n or 0) + 1


# ---------------------------------------------------------------------------
# Mapping
# ---------------------------------------------------------------------------


def _to_session(row: Any) -> Session:
    return Session(
        id=row.id,
        tenant_id=row.tenant_id,
        unit_id=row.unit_id,
        batch_id=row.batch_id,
        session_no=row.session_no,
        effective_revision_id=row.effective_revision_id,
        lifecycle_status=SessionLifecycleStatus(row.lifecycle_status),
        closure_id=row.closure_id,
        reschedule_reason=row.reschedule_reason,
        created_at=row.created_at,
        created_by=row.created_by,
        updated_at=row.updated_at,
        updated_by=row.updated_by,
    )


def _to_revision(row: Any) -> SessionRevision:
    return SessionRevision(
        id=row.id,
        tenant_id=row.tenant_id,
        session_id=row.session_id,
        revision_no=row.revision_no,
        starts_at=row.starts_at,
        ends_at=row.ends_at,
        timezone=row.timezone,
        location=row.location,
        change_reason=row.change_reason,
        published_at=row.published_at,
        created_at=row.created_at,
        created_by=row.created_by,
        updated_at=row.updated_at,
        updated_by=row.updated_by,
    )


def _to_attendance(row: Any) -> Attendance:
    return Attendance(
        id=row.id,
        tenant_id=row.tenant_id,
        session_id=row.session_id,
        enrollment_id=row.enrollment_id,
        attendance_code=AttendanceCode(row.attendance_code) if row.attendance_code else None,
        note=row.note,
        recorded_at=row.recorded_at,
        recorded_by=row.recorded_by,
        correction_reason=row.correction_reason,
        created_at=row.created_at,
        created_by=row.created_by,
        updated_at=row.updated_at,
        updated_by=row.updated_by,
    )


def _to_journal(row: Any) -> TeachingJournal:
    return TeachingJournal(
        id=row.id,
        tenant_id=row.tenant_id,
        session_id=row.session_id,
        actual_instructor_id=row.actual_instructor_id,
        topic=row.topic,
        activities=row.activities,
        internal_note=row.internal_note,
        portal_summary=row.portal_summary,
        follow_up=row.follow_up,
        created_at=row.created_at,
        created_by=row.created_by,
        updated_at=row.updated_at,
        updated_by=row.updated_by,
    )


def _to_completion(row: Any) -> SessionCompletion:
    return SessionCompletion(
        id=row.id,
        tenant_id=row.tenant_id,
        session_id=row.session_id,
        completion_no=row.completion_no,
        decision=row.decision,
        checklist_snapshot=row.checklist_snapshot,
        policy_id=row.policy_id,
        supersedes_id=row.supersedes_id,
        correction_reason=row.correction_reason,
        outcome=CompletionDecision(row.outcome),
        created_at=row.created_at,
        created_by=row.created_by,
        updated_at=row.updated_at,
        updated_by=row.updated_by,
    )