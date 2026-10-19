"""Batch application services.

Showpiece: ``place_enrollment`` implements the V05 capacity race using
``SELECT … FOR UPDATE`` on the ``batch`` row + active-enrollment count + INSERT
into ``enrollment`` (which is also constrained by the Postgres exclusion
constraint on overlapping effective_range). Concurrent placements race safely.
"""
from __future__ import annotations

from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from ksma.batch.domain import (
    Application,
    ApplicationStatus,
    AssignmentKind,
    AssignmentStatus,
    Batch,
    BatchStatus,
    ChangeKind,
    ChangeStatus,
    Completeness,
    DeliveryMode,
    Enrollment,
    EnrollmentChange,
    EnrollmentStatus,
    TeachingAssignment,
)
from ksma.batch.domain.events import (
    ApplicationApproved,
    ApplicationCreated,
    ApplicationRejected,
    ApplicationSubmitted,
    ApplicationWithdrawn,
    BatchCapacityChanged,
    BatchCapacityExceptionRecorded,
    BatchClosed,
    BatchCreated,
    BatchOpened,
    EnrollmentPaused,
    EnrollmentResumed,
    EnrollmentTransferred,
    EnrollmentWithdrawn,
    ParticipantEnrolled,
    TeachingAssignmentCreated,
    TeachingAssignmentEnded,
)
from ksma.batch.infrastructure.repositories import (
    ApplicationRepository,
    BatchRepository,
    EnrollmentChangeRepository,
    EnrollmentRepository,
    TeachingAssignmentRepository,
)
from ksma.program.domain import ProgramVersionStatus
from ksma.program.infrastructure.repositories import ProgramVersionRepository
from ksma.shared.errors import (
    CapacityExceededError,
    DomainError,
    StateConflictError,
    ValidationError,
)
from ksma.shared.events import bus
from ksma.shared.outbox import enqueue
from ksma.shared.tenancy import TenantContext


# ---------------------------------------------------------------------------
# Batch
# ---------------------------------------------------------------------------


async def create_batch(
    session: AsyncSession,
    ctx: TenantContext,
    *,
    unit_id: UUID,
    batch_code: str,
    program_version_id: UUID,
    capacity: int,
    delivery_mode: DeliveryMode,
    period_id: UUID | None = None,
    service_from: Any | None = None,
    service_until: Any | None = None,
) -> Batch:
    if capacity <= 0:
        raise ValidationError("capacity must be positive")
    # Per Q39(a) + CLS-A04: Batch must reference a published ProgramVersion.
    prepo = ProgramVersionRepository(session, ctx.tenant_id)
    pv = await prepo.get(program_version_id)
    if pv is None:
        raise ValidationError("ProgramVersion not found")
    if pv.status is not ProgramVersionStatus.PUBLISHED:
        raise StateConflictError(
            "Cannot create a batch against a non-published ProgramVersion"
        )
    brepo = BatchRepository(session, ctx.tenant_id)
    b = Batch(
        tenant_id=ctx.tenant_id,
        unit_id=unit_id,
        batch_code=batch_code,
        program_version_id=program_version_id,
        period_id=period_id,
        capacity=capacity,
        delivery_mode=delivery_mode,
        service_from=service_from,
        service_until=service_until,
        status=BatchStatus.PLANNED,
        created_by=str(ctx.account_id),
        updated_by=str(ctx.account_id),
    )
    await brepo.create(b)
    event = BatchCreated(tenant_id=ctx.tenant_id, actor_id=ctx.account_id)
    await enqueue(session, event, aggregate_ref=f"batch:{b.id}")
    bus().dispatch(event)
    return b


async def open_batch(
    session: AsyncSession, ctx: TenantContext, batch_id: UUID
) -> None:
    brepo = BatchRepository(session, ctx.tenant_id)
    b = await brepo.get(batch_id)
    if b is None:
        raise ValidationError("Batch not found")
    if b.status is not BatchStatus.PLANNED:
        raise StateConflictError(f"Cannot open batch in status {b.status.value}")
    await brepo.set_status(batch_id, status=BatchStatus.OPEN, actor=str(ctx.account_id))
    event = BatchOpened(tenant_id=ctx.tenant_id, actor_id=ctx.account_id)
    await enqueue(session, event, aggregate_ref=f"batch:{batch_id}")
    bus().dispatch(event)


async def close_batch(
    session: AsyncSession, ctx: TenantContext, batch_id: UUID
) -> None:
    brepo = BatchRepository(session, ctx.tenant_id)
    await brepo.set_status(batch_id, status=BatchStatus.CLOSED, actor=str(ctx.account_id))
    event = BatchClosed(tenant_id=ctx.tenant_id, actor_id=ctx.account_id)
    await enqueue(session, event, aggregate_ref=f"batch:{batch_id}")
    bus().dispatch(event)


async def change_batch_capacity(
    session: AsyncSession,
    ctx: TenantContext,
    *,
    batch_id: UUID,
    new_capacity: int,
    force: bool = False,
    reason: str | None = None,
) -> None:
    """Q46: lowering below active occupancy is rejected unless ``force=True``."""
    if new_capacity <= 0:
        raise ValidationError("capacity must be positive")
    brepo = BatchRepository(session, ctx.tenant_id)
    b = await brepo.lock_for_update(batch_id)
    if b is None:
        raise ValidationError("Batch not found")
    active_count = await brepo.count_active_enrollments(batch_id)
    if new_capacity < active_count:
        if not force:
            raise CapacityExceededError(
                f"New capacity {new_capacity} < active occupancy {active_count}; "
                "use force=true with a reason to record an exception",
                details={"new_capacity": new_capacity, "active": active_count},
            )
        if not reason:
            raise ValidationError("reason is required when force=true")
        # Audit the exception regardless of subsequent write.
        event = BatchCapacityExceptionRecorded(tenant_id=ctx.tenant_id, actor_id=ctx.account_id)
        await enqueue(
            session,
            event,
            aggregate_ref=f"batch:{batch_id}:capacity_exception",
        )
        bus().dispatch(event)
    await brepo.set_capacity(batch_id, capacity=new_capacity, actor=str(ctx.account_id))
    event = BatchCapacityChanged(tenant_id=ctx.tenant_id, actor_id=ctx.account_id)
    await enqueue(session, event, aggregate_ref=f"batch:{batch_id}")
    bus().dispatch(event)


# ---------------------------------------------------------------------------
# Application (REG-A09 / REG-A04)
# ---------------------------------------------------------------------------


async def create_application(
    session: AsyncSession,
    ctx: TenantContext,
    *,
    unit_id: UUID,
    participant_id: UUID,
    program_id: UUID,
    requested_start: Any | None = None,
    completeness: Completeness | None = None,
    source_ref: str | None = None,
) -> Application:
    arepo = ApplicationRepository(session, ctx.tenant_id)
    a = Application(
        tenant_id=ctx.tenant_id,
        unit_id=unit_id,
        participant_id=participant_id,
        program_id=program_id,
        requested_start=requested_start,
        completeness=completeness or Completeness(),
        status=ApplicationStatus.DRAFT,
        source_ref=source_ref,
        created_by=str(ctx.account_id),
        updated_by=str(ctx.account_id),
    )
    await arepo.create(a)
    event = ApplicationCreated(tenant_id=ctx.tenant_id, actor_id=ctx.account_id)
    await enqueue(session, event, aggregate_ref=f"application:{a.id}")
    bus().dispatch(event)
    return a


async def submit_application(
    session: AsyncSession, ctx: TenantContext, application_id: UUID
) -> None:
    arepo = ApplicationRepository(session, ctx.tenant_id)
    a = await arepo.get(application_id)
    if a is None:
        raise ValidationError("Application not found")
    if a.status is not ApplicationStatus.DRAFT:
        raise StateConflictError(f"Cannot submit application in status {a.status.value}")
    await arepo.update_status(
        application_id,
        status=ApplicationStatus.SUBMITTED,
        decision=None,
        actor=str(ctx.account_id),
    )
    event = ApplicationSubmitted(tenant_id=ctx.tenant_id, actor_id=ctx.account_id)
    await enqueue(session, event, aggregate_ref=f"application:{application_id}")
    bus().dispatch(event)


async def decide_application(
    session: AsyncSession,
    ctx: TenantContext,
    *,
    application_id: UUID,
    decision: str,
    reason: str | None = None,
) -> Application:
    """REG-A04 — approve/reject. Approval does NOT auto-create enrollment."""
    if decision not in {"approve", "reject"}:
        raise ValidationError("decision must be 'approve' or 'reject'")
    arepo = ApplicationRepository(session, ctx.tenant_id)
    a = await arepo.get(application_id)
    if a is None:
        raise ValidationError("Application not found")
    if a.status not in {ApplicationStatus.SUBMITTED, ApplicationStatus.UNDER_REVIEW}:
        raise StateConflictError(
            f"Cannot decide application in status {a.status.value}; "
            "must be submitted or under_review"
        )
    new_status = (
        ApplicationStatus.APPROVED if decision == "approve" else ApplicationStatus.REJECTED
    )
    decision_payload = {
        "actor": str(ctx.account_id),
        "decision": decision,
        "reason": reason or "",
    }
    await arepo.update_status(
        application_id,
        status=new_status,
        decision=decision_payload,
        actor=str(ctx.account_id),
    )
    event = (
        ApplicationApproved(tenant_id=ctx.tenant_id, actor_id=ctx.account_id)
        if new_status is ApplicationStatus.APPROVED
        else ApplicationRejected(tenant_id=ctx.tenant_id, actor_id=ctx.account_id)
    )
    await enqueue(session, event, aggregate_ref=f"application:{application_id}")
    bus().dispatch(event)
    a.status = new_status
    a.decision = decision_payload
    return a


async def withdraw_application(
    session: AsyncSession, ctx: TenantContext, application_id: UUID
) -> None:
    arepo = ApplicationRepository(session, ctx.tenant_id)
    await arepo.update_status(
        application_id,
        status=ApplicationStatus.WITHDRAWN,
        decision={"actor": str(ctx.account_id), "reason": "withdrawn"},
        actor=str(ctx.account_id),
    )
    event = ApplicationWithdrawn(tenant_id=ctx.tenant_id, actor_id=ctx.account_id)
    await enqueue(session, event, aggregate_ref=f"application:{application_id}")
    bus().dispatch(event)


# ---------------------------------------------------------------------------
# Enrollment — capacity-race-safe placement
# ---------------------------------------------------------------------------


async def place_enrollment(
    session: AsyncSession,
    ctx: TenantContext,
    *,
    participant_id: UUID,
    batch_id: UUID,
    application_id: UUID | None = None,
    effective_from: Any,
    effective_until: Any | None = None,
) -> Enrollment:
    """REG-A06 placement. V05 / R06.

    Belt-and-braces: ``SELECT … FOR UPDATE`` on the batch + count, then INSERT
    into enrollment (also constrained by the Postgres exclusion constraint on
    overlapping ``effective_range`` for the same participant+batch).
    """
    brepo = BatchRepository(session, ctx.tenant_id)
    b = await brepo.lock_for_update(batch_id)
    if b is None:
        raise ValidationError("Batch not found")
    if b.status not in {BatchStatus.OPEN, BatchStatus.RUNNING}:
        raise StateConflictError(
            f"Cannot place enrollment on a batch in status {b.status.value}"
        )
    active_count = await brepo.count_active_enrollments(batch_id, at=effective_from)
    if active_count >= b.capacity:
        raise CapacityExceededError(
            "Batch is at capacity; try another batch or wait for an opening",
            details={"capacity": b.capacity, "active": active_count},
        )

    erepo = EnrollmentRepository(session, ctx.tenant_id)
    e = Enrollment(
        tenant_id=ctx.tenant_id,
        unit_id=b.unit_id,
        participant_id=participant_id,
        batch_id=batch_id,
        application_id=application_id,
        effective_from=effective_from,
        effective_until=effective_until,
        status=EnrollmentStatus.PLANNED,
        created_by=str(ctx.account_id),
        updated_by=str(ctx.account_id),
    )
    try:
        await erepo.create(e)
    except IntegrityError as exc:
        # Translate the exclusion-constraint violation (R07) to a domain error.
        msg = str(exc.orig)
        if "uq_enrollment_no_overlap" in msg or "exclusion_violation" in msg:
            raise CapacityExceededError(
                "An overlapping active enrollment already exists for this participant + batch",
                code="ENROLLMENT_OVERLAP",
            ) from exc
        raise

    # Kick the batch from PLANNED/OPEN into RUNNING once it has ≥1 active.
    if b.status is BatchStatus.OPEN:
        await brepo.set_status(batch_id, status=BatchStatus.RUNNING, actor="system")

    event = ParticipantEnrolled(tenant_id=ctx.tenant_id, actor_id=ctx.account_id)
    await enqueue(session, event, aggregate_ref=f"enrollment:{e.id}")
    bus().dispatch(event)
    return e


async def activate_enrollment(
    session: AsyncSession, ctx: TenantContext, enrollment_id: UUID
) -> None:
    erepo = EnrollmentRepository(session, ctx.tenant_id)
    await erepo.update_status(enrollment_id, status=EnrollmentStatus.ACTIVE, actor=str(ctx.account_id))


async def pause_enrollment(
    session: AsyncSession,
    ctx: TenantContext,
    *,
    enrollment_id: UUID,
    effective_at: Any,
    reason: str,
) -> None:
    erepo = EnrollmentRepository(session, ctx.tenant_id)
    ccrepo = EnrollmentChangeRepository(session, ctx.tenant_id)
    e = await erepo.get(enrollment_id)
    if e is None:
        raise ValidationError("Enrollment not found")
    if e.status is not EnrollmentStatus.ACTIVE:
        raise StateConflictError(f"Cannot pause enrollment in status {e.status.value}")
    await erepo.update_status(enrollment_id, status=EnrollmentStatus.PAUSED, actor=str(ctx.account_id))
    change = EnrollmentChange(
        tenant_id=ctx.tenant_id,
        source_enrollment_id=enrollment_id,
        change_kind=ChangeKind.PAUSE,
        effective_at=effective_at,
        decision={"actor": str(ctx.account_id), "reason": reason},
        status=ChangeStatus.APPLIED,
        created_by=str(ctx.account_id),
        updated_by=str(ctx.account_id),
    )
    await ccrepo.create(change)
    event = EnrollmentPaused(tenant_id=ctx.tenant_id, actor_id=ctx.account_id)
    await enqueue(session, event, aggregate_ref=f"enrollment:{enrollment_id}")
    bus().dispatch(event)


async def resume_enrollment(
    session: AsyncSession,
    ctx: TenantContext,
    *,
    enrollment_id: UUID,
    effective_at: Any,
) -> None:
    erepo = EnrollmentRepository(session, ctx.tenant_id)
    ccrepo = EnrollmentChangeRepository(session, ctx.tenant_id)
    e = await erepo.get(enrollment_id)
    if e is None:
        raise ValidationError("Enrollment not found")
    if e.status is not EnrollmentStatus.PAUSED:
        raise StateConflictError(f"Cannot resume enrollment in status {e.status.value}")
    await erepo.update_status(enrollment_id, status=EnrollmentStatus.ACTIVE, actor=str(ctx.account_id))
    change = EnrollmentChange(
        tenant_id=ctx.tenant_id,
        source_enrollment_id=enrollment_id,
        change_kind=ChangeKind.RESUME,
        effective_at=effective_at,
        decision={"actor": str(ctx.account_id)},
        status=ChangeStatus.APPLIED,
        created_by=str(ctx.account_id),
        updated_by=str(ctx.account_id),
    )
    await ccrepo.create(change)
    event = EnrollmentResumed(tenant_id=ctx.tenant_id, actor_id=ctx.account_id)
    await enqueue(session, event, aggregate_ref=f"enrollment:{enrollment_id}")
    bus().dispatch(event)


async def withdraw_enrollment(
    session: AsyncSession,
    ctx: TenantContext,
    *,
    enrollment_id: UUID,
    effective_at: Any,
    reason: str,
) -> None:
    erepo = EnrollmentRepository(session, ctx.tenant_id)
    ccrepo = EnrollmentChangeRepository(session, ctx.tenant_id)
    e = await erepo.get(enrollment_id)
    if e is None:
        raise ValidationError("Enrollment not found")
    if e.status not in {EnrollmentStatus.PLANNED, EnrollmentStatus.ACTIVE}:
        raise StateConflictError(
            f"Cannot withdraw enrollment in status {e.status.value}"
        )
    await erepo.update_status(
        enrollment_id,
        status=EnrollmentStatus.WITHDRAWN,
        effective_until=effective_at,
        actor=str(ctx.account_id),
    )
    change = EnrollmentChange(
        tenant_id=ctx.tenant_id,
        source_enrollment_id=enrollment_id,
        change_kind=ChangeKind.WITHDRAW,
        effective_at=effective_at,
        decision={"actor": str(ctx.account_id), "reason": reason},
        status=ChangeStatus.APPLIED,
        created_by=str(ctx.account_id),
        updated_by=str(ctx.account_id),
    )
    await ccrepo.create(change)
    event = EnrollmentWithdrawn(tenant_id=ctx.tenant_id, actor_id=ctx.account_id)
    await enqueue(session, event, aggregate_ref=f"enrollment:{enrollment_id}")
    bus().dispatch(event)


async def transfer_enrollment(
    session: AsyncSession,
    ctx: TenantContext,
    *,
    source_enrollment_id: UUID,
    target_batch_id: UUID,
    effective_at: Any,
    reason: str,
) -> Enrollment:
    """REG-A08 / REG-04. Source enrollment is closed; new one opens against target."""
    brepo = BatchRepository(session, ctx.tenant_id)
    erepo = EnrollmentRepository(session, ctx.tenant_id)
    ccrepo = EnrollmentChangeRepository(session, ctx.tenant_id)

    src = await erepo.get(source_enrollment_id)
    if src is None:
        raise ValidationError("Source enrollment not found")
    if src.status not in {EnrollmentStatus.PLANNED, EnrollmentStatus.ACTIVE}:
        raise StateConflictError(
            f"Cannot transfer enrollment in status {src.status.value}"
        )

    # Capacity check on the target (same V05 semantics).
    target = await brepo.lock_for_update(target_batch_id)
    if target is None:
        raise ValidationError("Target batch not found")
    if target.status not in {BatchStatus.OPEN, BatchStatus.RUNNING}:
        raise StateConflictError(
            f"Cannot transfer into a batch in status {target.status.value}"
        )
    active_count = await brepo.count_active_enrollments(target_batch_id, at=effective_at)
    if active_count >= target.capacity:
        raise CapacityExceededError(
            "Target batch is at capacity",
            details={"capacity": target.capacity, "active": active_count},
        )

    # Close source.
    await erepo.update_status(
        source_enrollment_id,
        status=EnrollmentStatus.TRANSFERRED,
        effective_until=effective_at,
        actor=str(ctx.account_id),
    )

    # Open target enrollment.
    new_e = Enrollment(
        tenant_id=ctx.tenant_id,
        unit_id=target.unit_id,
        participant_id=src.participant_id,
        batch_id=target_batch_id,
        application_id=src.application_id,
        effective_from=effective_at,
        effective_until=None,
        status=EnrollmentStatus.ACTIVE,
        created_by=str(ctx.account_id),
        updated_by=str(ctx.account_id),
    )
    try:
        await erepo.create(new_e)
    except IntegrityError as exc:
        if "exclusion_violation" in str(exc.orig):
            raise CapacityExceededError(
                "An overlapping active enrollment already exists for this participant + target batch",
                code="ENROLLMENT_OVERLAP",
            ) from exc
        raise

    change = EnrollmentChange(
        tenant_id=ctx.tenant_id,
        source_enrollment_id=source_enrollment_id,
        target_enrollment_id=new_e.id,
        change_kind=ChangeKind.TRANSFER,
        effective_at=effective_at,
        decision={"actor": str(ctx.account_id), "reason": reason},
        status=ChangeStatus.APPLIED,
        created_by=str(ctx.account_id),
        updated_by=str(ctx.account_id),
    )
    await ccrepo.create(change)
    event = EnrollmentTransferred(tenant_id=ctx.tenant_id, actor_id=ctx.account_id)
    await enqueue(session, event, aggregate_ref=f"enrollment:{new_e.id}")
    bus().dispatch(event)
    return new_e


# ---------------------------------------------------------------------------
# TeachingAssignment
# ---------------------------------------------------------------------------


async def assign_teacher(
    session: AsyncSession,
    ctx: TenantContext,
    *,
    unit_id: UUID,
    membership_id: UUID,
    assignment_kind: AssignmentKind,
    batch_id: UUID | None = None,
    session_id: UUID | None = None,
    component_codes: list[str] | None = None,
    valid_from: Any,
    valid_until: Any | None = None,
) -> TeachingAssignment:
    if (batch_id is None) == (session_id is None):
        raise ValidationError("Exactly one of batch_id or session_id must be provided")
    tarepo = TeachingAssignmentRepository(session, ctx.tenant_id)
    a = TeachingAssignment(
        tenant_id=ctx.tenant_id,
        unit_id=unit_id,
        membership_id=membership_id,
        batch_id=batch_id,
        session_id=session_id,
        assignment_kind=assignment_kind,
        component_codes=list(component_codes or []),
        valid_from=valid_from,
        valid_until=valid_until,
        status=AssignmentStatus.ACTIVE,
        created_by=str(ctx.account_id),
        updated_by=str(ctx.account_id),
    )
    await tarepo.create(a)
    event = TeachingAssignmentCreated(tenant_id=ctx.tenant_id, actor_id=ctx.account_id)
    await enqueue(session, event, aggregate_ref=f"teaching_assignment:{a.id}")
    bus().dispatch(event)
    return a


async def end_teaching_assignment(
    session: AsyncSession, ctx: TenantContext, assignment_id: UUID
) -> None:
    tarepo = TeachingAssignmentRepository(session, ctx.tenant_id)
    await tarepo.update_status(
        assignment_id, status=AssignmentStatus.ENDED, actor=str(ctx.account_id)
    )
    event = TeachingAssignmentEnded(tenant_id=ctx.tenant_id, actor_id=ctx.account_id)
    await enqueue(session, event, aggregate_ref=f"teaching_assignment:{assignment_id}")
    bus().dispatch(event)