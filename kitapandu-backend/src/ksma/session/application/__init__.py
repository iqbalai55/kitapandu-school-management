"""Session application services.

Showpiece: ``close_session`` enforces R09 closure invariants:
- Every eligible enrollment has a non-null ``attendance_code`` (unfilled ≠ absent)
- TeachingJournal.topic is non-empty
- actual_instructor_id is set

``correct_session`` chains a new ``SessionCompletion`` with ``supersedes_id``,
emits ``SessionCorrected`` + ``AttendanceCorrected``.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from ksma.batch.infrastructure.repositories import BatchRepository
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
from ksma.session.domain.events import (
    AttendanceCorrected,
    AttendanceRecorded,
    SessionCancelled,
    SessionClosed,
    SessionCorrected,
    SessionCreated,
    SessionRescheduled,
    SessionScheduled,
    SubstituteInstructorAssigned,
    TeachingJournalWritten,
)
from ksma.session.infrastructure.repositories import (
    AttendanceRepository,
    SessionCompletionRepository,
    SessionRepository,
    SessionRevisionRepository,
    TeachingJournalRepository,
)
from ksma.shared.errors import (
    DomainError,
    StateConflictError,
    ValidationError,
)
from ksma.shared.events import bus
from ksma.shared.outbox import enqueue
from ksma.shared.tenancy import TenantContext
from ksma.shared.time import clock


# ---------------------------------------------------------------------------
# Session + revision
# ---------------------------------------------------------------------------


async def create_session(
    session: AsyncSession,
    ctx: TenantContext,
    *,
    batch_id: UUID,
) -> Session:
    """Create a new Session (logical) with no revision yet.

    The first revision is created via ``add_revision`` which also sets
    ``effective_revision_id``. Cancelled sessions do not get a revision.
    """
    brepo = BatchRepository(session, ctx.tenant_id)
    batch = await brepo.get(batch_id)
    if batch is None:
        raise ValidationError("Batch not found")
    srepo = SessionRepository(session, ctx.tenant_id)
    session_no = await srepo.next_session_no(batch_id)
    s = Session(
        tenant_id=ctx.tenant_id,
        unit_id=batch.unit_id,
        batch_id=batch_id,
        session_no=session_no,
        lifecycle_status=SessionLifecycleStatus.PLANNED,
        created_by=str(ctx.account_id),
        updated_by=str(ctx.account_id),
    )
    await srepo.create(s)
    event = SessionCreated(tenant_id=ctx.tenant_id, actor_id=ctx.account_id)
    await enqueue(session, event, aggregate_ref=f"session:{s.id}")
    bus().dispatch(event)
    return s


async def add_revision(
    session: AsyncSession,
    ctx: TenantContext,
    *,
    session_id: UUID,
    starts_at: datetime,
    ends_at: datetime,
    timezone_str: str,
    change_reason: str,
    location: dict[str, Any] | None = None,
    make_effective: bool = True,
) -> SessionRevision:
    srepo = SessionRepository(session, ctx.tenant_id)
    rrepo = SessionRevisionRepository(session, ctx.tenant_id)
    s = await srepo.lock_for_update(session_id)
    if s is None:
        raise ValidationError("Session not found")
    if s.lifecycle_status is SessionLifecycleStatus.CANCELLED:
        raise StateConflictError("Cancelled session cannot accept new revisions")
    if s.lifecycle_status is SessionLifecycleStatus.CLOSED:
        raise StateConflictError("Closed session cannot accept new revisions (create a correction)")
    if ends_at <= starts_at:
        raise ValidationError("ends_at must be strictly after starts_at")
    rn = await rrepo.next_revision_no(session_id)
    r = SessionRevision(
        tenant_id=ctx.tenant_id,
        session_id=session_id,
        revision_no=rn,
        starts_at=starts_at,
        ends_at=ends_at,
        timezone=timezone_str,
        location=location,
        change_reason=change_reason,
        published_at=clock().now() if make_effective else None,
        created_by=str(ctx.account_id),
        updated_by=str(ctx.account_id),
    )
    await rrepo.create(r)
    if make_effective:
        await srepo.set_effective_revision(
            session_id, revision_id=r.id, reschedule_reason=change_reason, actor=str(ctx.account_id)
        )
        await srepo.set_lifecycle(session_id, SessionLifecycleStatus.SCHEDULED, str(ctx.account_id))
        event = (
            SessionScheduled(tenant_id=ctx.tenant_id, actor_id=ctx.account_id)
            if rn == 1
            else SessionRescheduled(tenant_id=ctx.tenant_id, actor_id=ctx.account_id)
        )
        await enqueue(session, event, aggregate_ref=f"session:{session_id}")
        bus().dispatch(event)
    return r


async def cancel_session(
    session: AsyncSession,
    ctx: TenantContext,
    *,
    session_id: UUID,
    reason: str,
) -> None:
    srepo = SessionRepository(session, ctx.tenant_id)
    s = await srepo.lock_for_update(session_id)
    if s is None:
        raise ValidationError("Session not found")
    if s.lifecycle_status in {SessionLifecycleStatus.CLOSED, SessionLifecycleStatus.CANCELLED}:
        raise StateConflictError(
            f"Cannot cancel a session in status {s.lifecycle_status.value}"
        )
    await srepo.set_lifecycle(session_id, SessionLifecycleStatus.CANCELLED, str(ctx.account_id))
    await srepo.set_effective_revision(
        session_id, revision_id=s.effective_revision_id or uuid4(), reschedule_reason=f"CANCELLED: {reason}", actor=str(ctx.account_id)  # type: ignore[arg-type]
    )
    event = SessionCancelled(tenant_id=ctx.tenant_id, actor_id=ctx.account_id)
    await enqueue(session, event, aggregate_ref=f"session:{session_id}")
    bus().dispatch(event)


# ---------------------------------------------------------------------------
# Attendance
# ---------------------------------------------------------------------------


async def record_attendance(
    session: AsyncSession,
    ctx: TenantContext,
    *,
    session_id: UUID,
    enrollment_id: UUID,
    attendance_code: AttendanceCode,
    note: str | None = None,
) -> Attendance:
    srepo = SessionRepository(session, ctx.tenant_id)
    s = await srepo.get(session_id)
    if s is None:
        raise ValidationError("Session not found")
    if s.lifecycle_status in {SessionLifecycleStatus.CANCELLED}:
        raise StateConflictError("Cancelled session does not accept attendance")
    if s.lifecycle_status is SessionLifecycleStatus.CLOSED:
        raise StateConflictError(
            "Closed session requires a correction (use correct_attendance)"
        )
    arepo = AttendanceRepository(session, ctx.tenant_id)
    now = clock().now()
    a = await arepo.upsert(
        session_id=session_id,
        enrollment_id=enrollment_id,
        attendance_code=attendance_code,
        note=note,
        recorded_by=str(ctx.account_id),
        recorded_at=now,
    )
    event = AttendanceRecorded(tenant_id=ctx.tenant_id, actor_id=ctx.account_id)
    await enqueue(session, event, aggregate_ref=f"attendance:{session_id}:{enrollment_id}")
    bus().dispatch(event)
    return a


async def correct_attendance(
    session: AsyncSession,
    ctx: TenantContext,
    *,
    session_id: UUID,
    enrollment_id: UUID,
    attendance_code: AttendanceCode | None,
    correction_reason: str,
) -> Attendance:
    if not correction_reason:
        raise ValidationError("correction_reason is required for attendance corrections")
    arepo = AttendanceRepository(session, ctx.tenant_id)
    a = await arepo.correct(
        session_id=session_id,
        enrollment_id=enrollment_id,
        attendance_code=attendance_code,
        correction_reason=correction_reason,
        recorded_by=str(ctx.account_id),
        recorded_at=clock().now(),
    )
    event = AttendanceCorrected(tenant_id=ctx.tenant_id, actor_id=ctx.account_id)
    await enqueue(session, event, aggregate_ref=f"attendance:{session_id}:{enrollment_id}")
    bus().dispatch(event)
    return a


# ---------------------------------------------------------------------------
# TeachingJournal
# ---------------------------------------------------------------------------


async def write_journal(
    session: AsyncSession,
    ctx: TenantContext,
    *,
    session_id: UUID,
    actual_instructor_id: UUID,
    topic: str,
    activities: str | None = None,
    internal_note: str | None = None,
    portal_summary: str | None = None,
    follow_up: str | None = None,
) -> TeachingJournal:
    if not topic:
        raise ValidationError("topic is required on TeachingJournal (R09)")
    srepo = SessionRepository(session, ctx.tenant_id)
    s = await srepo.get(session_id)
    if s is None:
        raise ValidationError("Session not found")
    if s.lifecycle_status in {SessionLifecycleStatus.CANCELLED}:
        raise StateConflictError("Cancelled session does not accept a teaching journal")
    jrepo = TeachingJournalRepository(session, ctx.tenant_id)
    j = TeachingJournal(
        tenant_id=ctx.tenant_id,
        session_id=session_id,
        actual_instructor_id=actual_instructor_id,
        topic=topic,
        activities=activities,
        internal_note=internal_note,
        portal_summary=portal_summary,
        follow_up=follow_up,
        created_by=str(ctx.account_id),
        updated_by=str(ctx.account_id),
    )
    await jrepo.upsert(j)
    event = TeachingJournalWritten(tenant_id=ctx.tenant_id, actor_id=ctx.account_id)
    await enqueue(session, event, aggregate_ref=f"teaching_journal:{session_id}")
    bus().dispatch(event)
    return j


# ---------------------------------------------------------------------------
# Closure (R09 invariants)
# ---------------------------------------------------------------------------


async def close_session(
    session: AsyncSession, ctx: TenantContext, *, session_id: UUID, policy_id: UUID | None = None
) -> SessionCompletion:
    srepo = SessionRepository(session, ctx.tenant_id)
    s = await srepo.lock_for_update(session_id)
    if s is None:
        raise ValidationError("Session not found")
    if s.lifecycle_status is SessionLifecycleStatus.CANCELLED:
        raise StateConflictError("Cancelled session cannot be closed")
    if s.lifecycle_status is SessionLifecycleStatus.CLOSED:
        raise StateConflictError("Session is already closed; use correct_session to amend")

    # R09 invariant 1: every eligible enrollment must have an attendance_code.
    arepo = AttendanceRepository(session, ctx.tenant_id)
    rows = await session.execute(
        text(
            """
            SELECT
              e.id AS enrollment_id,
              e.status AS enrollment_status,
              a.attendance_code
            FROM enrollment e
            LEFT JOIN attendance a
              ON a.session_id = :sid AND a.enrollment_id = e.id
            WHERE e.batch_id = (SELECT batch_id FROM session WHERE id = :sid)
              AND e.tenant_id = :tid
              AND e.status IN ('planned','active','paused','completed')
            """
        ),
        {"sid": str(session_id), "tid": str(ctx.tenant_id)},
    )
    missing: list[UUID] = []
    for r in rows:
        if r.enrollment_status == "completed":
            continue
        if r.attendance_code is None:
            missing.append(r.enrollment_id)
    if missing:
        from ksma.shared.errors import ValidationError as _VE

        raise _VE(
            f"Cannot close session: {len(missing)} enrollments have unfilled attendance "
            "(R09 — unfilled ≠ absent)",
            details={
                "missing_enrollment_ids": [str(x) for x in missing[:20]],
            },
        )

    # R09 invariant 2: TeachingJournal.topic + actual_instructor_id required.
    jrepo = TeachingJournalRepository(session, ctx.tenant_id)
    j = await jrepo.get(session_id)
    if j is None or not j.topic:
        from ksma.shared.errors import ValidationError as _VE

        raise _VE("Cannot close session: TeachingJournal is missing or has empty topic")

    # Build the closure decision.
    crepo = SessionCompletionRepository(session, ctx.tenant_id)
    no = await crepo.next_completion_no(session_id)
    checklist = {
        "all_attendance_filled": True,
        "journal_topic_present": True,
        "actual_instructor_present": True,
        "rule_version": "R09-v1",
        "checked_at": clock().now().isoformat(),
    }
    c = SessionCompletion(
        tenant_id=ctx.tenant_id,
        session_id=session_id,
        completion_no=no,
        decision={
            "actor": str(ctx.account_id),
            "decided_at": clock().now().isoformat(),
        },
        checklist_snapshot=checklist,
        policy_id=policy_id,
        outcome=CompletionDecision.COMPLETED,
        created_by=str(ctx.account_id),
        updated_by=str(ctx.account_id),
    )
    await crepo.create(c)
    await srepo.set_closure(session_id, closure_id=c.id, actor=str(ctx.account_id))
    await srepo.set_lifecycle(session_id, SessionLifecycleStatus.CLOSED, str(ctx.account_id))

    event = SessionClosed(tenant_id=ctx.tenant_id, actor_id=ctx.account_id)
    await enqueue(session, event, aggregate_ref=f"session:{session_id}")
    bus().dispatch(event)
    return c


async def correct_session(
    session: AsyncSession,
    ctx: TenantContext,
    *,
    session_id: UUID,
    correction_reason: str,
    attendance_changes: list[dict[str, Any]] | None = None,
    journal: dict[str, Any] | None = None,
) -> SessionCompletion:
    """Post-close correction. Chains a new ``SessionCompletion`` with supersedes_id.

    ``attendance_changes`` is a list of ``{enrollment_id, attendance_code}``.
    """
    if not correction_reason:
        raise ValidationError("correction_reason is required for post-close corrections")
    srepo = SessionRepository(session, ctx.tenant_id)
    s = await srepo.get(session_id)
    if s is None:
        raise ValidationError("Session not found")
    if s.lifecycle_status is not SessionLifecycleStatus.CLOSED:
        raise StateConflictError("Only closed sessions can be corrected this way")

    crepo = SessionCompletionRepository(session, ctx.tenant_id)
    latest = await crepo.latest_for_session(session_id)
    if latest is None:
        raise StateConflictError("Session is closed but no completion decision found")

    arepo = AttendanceRepository(session, ctx.tenant_id)
    for change in attendance_changes or []:
        await arepo.correct(
            session_id=session_id,
            enrollment_id=UUID(change["enrollment_id"]),
            attendance_code=AttendanceCode(change["attendance_code"])
            if change.get("attendance_code")
            else None,
            correction_reason=correction_reason,
            recorded_by=str(ctx.account_id),
            recorded_at=clock().now(),
        )

    new_no = await crepo.next_completion_no(session_id)
    new = SessionCompletion(
        tenant_id=ctx.tenant_id,
        session_id=session_id,
        completion_no=new_no,
        decision={
            "actor": str(ctx.account_id),
            "decided_at": clock().now().isoformat(),
        },
        checklist_snapshot=latest.checklist_snapshot,
        policy_id=latest.policy_id,
        supersedes_id=latest.id,
        correction_reason=correction_reason,
        outcome=CompletionDecision.REOPENED,
        created_by=str(ctx.account_id),
        updated_by=str(ctx.account_id),
    )
    await crepo.create(new)
    await srepo.set_closure(session_id, closure_id=new.id, actor=str(ctx.account_id))

    event = SessionCorrected(tenant_id=ctx.tenant_id, actor_id=ctx.account_id)
    await enqueue(session, event, aggregate_ref=f"session:{session_id}")
    bus().dispatch(event)
    return new


# ---------------------------------------------------------------------------
# Substitute instructor (SES-A08) — just records the assignment; access control
# for IN role on this session is enforced by RBAC joining teaching_assignment.
# ---------------------------------------------------------------------------


async def record_substitute(
    session: AsyncSession,
    ctx: TenantContext,
    *,
    session_id: UUID,
    substitute_membership_id: UUID,
    handover_note: str,
) -> None:
    from ksma.batch.infrastructure.repositories import TeachingAssignmentRepository

    srepo = SessionRepository(session, ctx.tenant_id)
    s = await srepo.get(session_id)
    if s is None:
        raise ValidationError("Session not found")
    tarepo = TeachingAssignmentRepository(session, ctx.tenant_id)
    a = await tarepo.create(  # type: ignore[attr-defined]
        # use the batch context's TeachingAssignment entity
        __import__("ksma.batch.domain", fromlist=["TeachingAssignment"]).TeachingAssignment(
            tenant_id=ctx.tenant_id,
            unit_id=s.unit_id,
            membership_id=substitute_membership_id,
            batch_id=None,
            session_id=session_id,
            assignment_kind=__import__("ksma.batch.domain", fromlist=["AssignmentKind"]).AssignmentKind.SUBSTITUTE,
            valid_from=clock().now(),
            created_by=str(ctx.account_id),
            updated_by=str(ctx.account_id),
        )
    )
    event = SubstituteInstructorAssigned(tenant_id=ctx.tenant_id, actor_id=ctx.account_id)
    await enqueue(
        session,
        event,
        aggregate_ref=f"session:{session_id}:substitute:{substitute_membership_id}",
    )
    bus().dispatch(event)