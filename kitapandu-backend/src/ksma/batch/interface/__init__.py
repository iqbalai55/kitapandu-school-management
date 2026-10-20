"""Batch FastAPI router."""
from __future__ import annotations

from datetime import date, datetime
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, ConfigDict, Field

from ksma.batch.application import (
    activate_enrollment,
    assign_teacher,
    change_batch_capacity,
    close_batch,
    create_application,
    create_batch,
    decide_application,
    end_teaching_assignment,
    open_batch,
    pause_enrollment,
    place_enrollment,
    resume_enrollment,
    submit_application,
    transfer_enrollment,
    withdraw_application,
    withdraw_enrollment,
)
from ksma.batch.domain import (
    ApplicationStatus,
    AssignmentKind,
    BatchStatus,
    EnrollmentStatus,
)
from ksma.batch.infrastructure.repositories import (
    ApplicationRepository,
    BatchRepository,
    EnrollmentRepository,
    TeachingAssignmentRepository,
)
from ksma.program.domain import DeliveryMode as _DeliveryMode
from ksma.shared.persistence import session_scope
from ksma.shared.tenancy import TenantContext
from ksma.shared.auth.dependencies import tenant_context_dep as require_tenant_context

router = APIRouter(prefix="/api/v1", tags=["batch"])



# --- Schemas -----------------------------------------------------------------


class CreateBatchBody(BaseModel):
    unit_id: UUID
    batch_code: str = Field(min_length=1, max_length=40)
    program_version_id: UUID
    period_id: UUID | None = None
    capacity: int = Field(ge=1)
    delivery_mode: _DeliveryMode
    service_from: date | None = None
    service_until: date | None = None


class BatchOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    unit_id: UUID
    batch_code: str
    program_version_id: UUID
    period_id: UUID | None = None
    capacity: int
    delivery_mode: str
    service_from: date | None = None
    service_until: date | None = None
    status: str
    active_enrollments: int | None = None


class BatchCapacityBody(BaseModel):
    capacity: int = Field(ge=1)
    force: bool = False
    reason: str | None = Field(default=None, max_length=1000)


class CreateApplicationBody(BaseModel):
    unit_id: UUID
    participant_id: UUID
    program_id: UUID
    requested_start: date | None = None
    completeness: list[dict[str, Any]] = Field(default_factory=list)
    source_ref: str | None = Field(default=None, max_length=120)


class ApplicationOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    unit_id: UUID
    participant_id: UUID
    program_id: UUID
    requested_start: date | None = None
    status: str
    decision: dict[str, Any] | None = None


class DecideApplicationBody(BaseModel):
    decision: str = Field(pattern="^(approve|reject)$")
    reason: str | None = Field(default=None, max_length=1000)


class PlaceEnrollmentBody(BaseModel):
    participant_id: UUID
    batch_id: UUID
    application_id: UUID | None = None
    effective_from: datetime
    effective_until: datetime | None = None


class EnrollmentOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    participant_id: UUID
    batch_id: UUID
    application_id: UUID | None = None
    status: str
    effective_from: datetime | None = None
    effective_until: datetime | None = None


class EnrollmentChangeBody(BaseModel):
    effective_at: datetime
    reason: str = Field(min_length=1, max_length=1000)


class TransferBody(BaseModel):
    source_enrollment_id: UUID
    target_batch_id: UUID
    effective_at: datetime
    reason: str = Field(min_length=1, max_length=1000)


class AssignTeacherBody(BaseModel):
    unit_id: UUID
    membership_id: UUID
    assignment_kind: AssignmentKind
    batch_id: UUID | None = None
    session_id: UUID | None = None
    component_codes: list[str] = Field(default_factory=list)
    valid_from: datetime
    valid_until: datetime | None = None


class TeachingAssignmentOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    membership_id: UUID
    batch_id: UUID | None = None
    session_id: UUID | None = None
    assignment_kind: str
    valid_from: datetime | None = None
    valid_until: datetime | None = None
    status: str


# --- Batch endpoints ---------------------------------------------------------


@router.post("/batches", response_model=BatchOut, status_code=status.HTTP_201_CREATED)
async def post_batch(
    body: CreateBatchBody,
    ctx: TenantContext = Depends(require_tenant_context),
) -> BatchOut:
    async with session_scope() as session:
        b = await create_batch(
            session,
            ctx,
            unit_id=body.unit_id,
            batch_code=body.batch_code,
            program_version_id=body.program_version_id,
            capacity=body.capacity,
            delivery_mode=body.delivery_mode,
            period_id=body.period_id,
            service_from=body.service_from,
            service_until=body.service_until,
        )
        return BatchOut(
            id=b.id,
            unit_id=b.unit_id,
            batch_code=b.batch_code,
            program_version_id=b.program_version_id,
            period_id=b.period_id,
            capacity=b.capacity,
            delivery_mode=b.delivery_mode.value,
            service_from=b.service_from,
            service_until=b.service_until,
            status=b.status.value,
        )


@router.get("/batches", response_model=list[BatchOut])
async def get_batches(
    unit_id: UUID | None = None,
    status_filter: BatchStatus | None = Query(default=None, alias="status"),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    ctx: TenantContext = Depends(require_tenant_context),
) -> list[BatchOut]:
    async with session_scope() as session:
        repo = BatchRepository(session, ctx.tenant_id)
        rows = await repo.list(unit_id=unit_id, status=status_filter, limit=limit, offset=offset)
        out: list[BatchOut] = []
        for b in rows:
            active = await repo.count_active_enrollments(b.id)
            out.append(
                BatchOut(
                    id=b.id,
                    unit_id=b.unit_id,
                    batch_code=b.batch_code,
                    program_version_id=b.program_version_id,
                    period_id=b.period_id,
                    capacity=b.capacity,
                    delivery_mode=b.delivery_mode.value,
                    service_from=b.service_from,
                    service_until=b.service_until,
                    status=b.status.value,
                    active_enrollments=active,
                )
            )
        return out


@router.get("/batches/{batch_id}", response_model=BatchOut)
async def get_batch(
    batch_id: UUID,
    ctx: TenantContext = Depends(require_tenant_context),
) -> BatchOut:
    async with session_scope() as session:
        repo = BatchRepository(session, ctx.tenant_id)
        b = await repo.get(batch_id)
        if b is None:
            raise HTTPException(status_code=404, detail="not found")
        active = await repo.count_active_enrollments(b.id)
        return BatchOut(
            id=b.id,
            unit_id=b.unit_id,
            batch_code=b.batch_code,
            program_version_id=b.program_version_id,
            period_id=b.period_id,
            capacity=b.capacity,
            delivery_mode=b.delivery_mode.value,
            service_from=b.service_from,
            service_until=b.service_until,
            status=b.status.value,
            active_enrollments=active,
        )


@router.post("/batches/{batch_id}/open", status_code=status.HTTP_204_NO_CONTENT)
async def post_open_batch(
    batch_id: UUID,
    ctx: TenantContext = Depends(require_tenant_context),
) -> None:
    async with session_scope() as session:
        await open_batch(session, ctx, batch_id)


@router.post("/batches/{batch_id}/close", status_code=status.HTTP_204_NO_CONTENT)
async def post_close_batch(
    batch_id: UUID,
    ctx: TenantContext = Depends(require_tenant_context),
) -> None:
    async with session_scope() as session:
        await close_batch(session, ctx, batch_id)


@router.post("/batches/{batch_id}/capacity", response_model=BatchOut)
async def post_change_capacity(
    batch_id: UUID,
    body: BatchCapacityBody,
    ctx: TenantContext = Depends(require_tenant_context),
) -> BatchOut:
    async with session_scope() as session:
        await change_batch_capacity(
            session,
            ctx,
            batch_id=batch_id,
            new_capacity=body.capacity,
            force=body.force,
            reason=body.reason,
        )
        repo = BatchRepository(session, ctx.tenant_id)
        b = await repo.get(batch_id)
        active = await repo.count_active_enrollments(b.id)  # type: ignore[union-attr]
        return BatchOut(
            id=b.id,  # type: ignore[union-attr]
            unit_id=b.unit_id,
            batch_code=b.batch_code,
            program_version_id=b.program_version_id,
            period_id=b.period_id,
            capacity=b.capacity,
            delivery_mode=b.delivery_mode.value,
            service_from=b.service_from,
            service_until=b.service_until,
            status=b.status.value,
            active_enrollments=active,
        )


# --- Application endpoints ---------------------------------------------------


@router.post("/applications", response_model=ApplicationOut, status_code=status.HTTP_201_CREATED)
async def post_application(
    body: CreateApplicationBody,
    ctx: TenantContext = Depends(require_tenant_context),
) -> ApplicationOut:
    async with session_scope() as session:
        from ksma.batch.domain import Completeness as _Comp

        a = await create_application(
            session,
            ctx,
            unit_id=body.unit_id,
            participant_id=body.participant_id,
            program_id=body.program_id,
            requested_start=body.requested_start,
            completeness=_Comp(items=body.completeness),
            source_ref=body.source_ref,
        )
        return ApplicationOut(
            id=a.id,
            unit_id=a.unit_id,
            participant_id=a.participant_id,
            program_id=a.program_id,
            requested_start=a.requested_start,
            status=a.status.value,
            decision=a.decision,
        )


@router.post("/applications/{application_id}/submit", status_code=status.HTTP_204_NO_CONTENT)
async def post_submit_application(
    application_id: UUID,
    ctx: TenantContext = Depends(require_tenant_context),
) -> None:
    async with session_scope() as session:
        await submit_application(session, ctx, application_id)


@router.post("/applications/{application_id}/decide", response_model=ApplicationOut)
async def post_decide_application(
    application_id: UUID,
    body: DecideApplicationBody,
    ctx: TenantContext = Depends(require_tenant_context),
) -> ApplicationOut:
    async with session_scope() as session:
        a = await decide_application(
            session,
            ctx,
            application_id=application_id,
            decision=body.decision,
            reason=body.reason,
        )
        return ApplicationOut(
            id=a.id,
            unit_id=a.unit_id,
            participant_id=a.participant_id,
            program_id=a.program_id,
            requested_start=a.requested_start,
            status=a.status.value,
            decision=a.decision,
        )


@router.post("/applications/{application_id}/withdraw", status_code=status.HTTP_204_NO_CONTENT)
async def post_withdraw_application(
    application_id: UUID,
    ctx: TenantContext = Depends(require_tenant_context),
) -> None:
    async with session_scope() as session:
        await withdraw_application(session, ctx, application_id)


@router.get("/applications", response_model=list[ApplicationOut])
async def get_applications(
    status_filter: ApplicationStatus | None = Query(default=None, alias="status"),
    participant_id: UUID | None = None,
    unit_id: UUID | None = None,
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    ctx: TenantContext = Depends(require_tenant_context),
) -> list[ApplicationOut]:
    async with session_scope() as session:
        repo = ApplicationRepository(session, ctx.tenant_id)
        rows = await repo.list(
            status=status_filter,
            participant_id=participant_id,
            unit_id=unit_id,
            limit=limit,
            offset=offset,
        )
        return [
            ApplicationOut(
                id=a.id,
                unit_id=a.unit_id,
                participant_id=a.participant_id,
                program_id=a.program_id,
                requested_start=a.requested_start,
                status=a.status.value,
                decision=a.decision,
            )
            for a in rows
        ]


# --- Enrollment endpoints ----------------------------------------------------


@router.post("/enrollments", response_model=EnrollmentOut, status_code=status.HTTP_201_CREATED)
async def post_enrollment(
    body: PlaceEnrollmentBody,
    ctx: TenantContext = Depends(require_tenant_context),
) -> EnrollmentOut:
    async with session_scope() as session:
        e = await place_enrollment(
            session,
            ctx,
            participant_id=body.participant_id,
            batch_id=body.batch_id,
            application_id=body.application_id,
            effective_from=body.effective_from,
            effective_until=body.effective_until,
        )
        return EnrollmentOut(
            id=e.id,
            participant_id=e.participant_id,
            batch_id=e.batch_id,
            application_id=e.application_id,
            status=e.status.value,
            effective_from=e.effective_from,
            effective_until=e.effective_until,
        )


@router.post("/enrollments/{enrollment_id}/activate", status_code=status.HTTP_204_NO_CONTENT)
async def post_activate_enrollment(
    enrollment_id: UUID,
    ctx: TenantContext = Depends(require_tenant_context),
) -> None:
    async with session_scope() as session:
        await activate_enrollment(session, ctx, enrollment_id)


@router.post("/enrollments/{enrollment_id}/pause", status_code=status.HTTP_204_NO_CONTENT)
async def post_pause_enrollment(
    enrollment_id: UUID,
    body: EnrollmentChangeBody,
    ctx: TenantContext = Depends(require_tenant_context),
) -> None:
    async with session_scope() as session:
        await pause_enrollment(
            session,
            ctx,
            enrollment_id=enrollment_id,
            effective_at=body.effective_at,
            reason=body.reason,
        )


@router.post("/enrollments/{enrollment_id}/resume", status_code=status.HTTP_204_NO_CONTENT)
async def post_resume_enrollment(
    enrollment_id: UUID,
    body: EnrollmentChangeBody,
    ctx: TenantContext = Depends(require_tenant_context),
) -> None:
    async with session_scope() as session:
        await resume_enrollment(
            session,
            ctx,
            enrollment_id=enrollment_id,
            effective_at=body.effective_at,
        )


@router.post("/enrollments/{enrollment_id}/withdraw", status_code=status.HTTP_204_NO_CONTENT)
async def post_withdraw_enrollment(
    enrollment_id: UUID,
    body: EnrollmentChangeBody,
    ctx: TenantContext = Depends(require_tenant_context),
) -> None:
    async with session_scope() as session:
        await withdraw_enrollment(
            session,
            ctx,
            enrollment_id=enrollment_id,
            effective_at=body.effective_at,
            reason=body.reason,
        )


@router.post("/enrollments/transfer", response_model=EnrollmentOut)
async def post_transfer_enrollment(
    body: TransferBody,
    ctx: TenantContext = Depends(require_tenant_context),
) -> EnrollmentOut:
    async with session_scope() as session:
        e = await transfer_enrollment(
            session,
            ctx,
            source_enrollment_id=body.source_enrollment_id,
            target_batch_id=body.target_batch_id,
            effective_at=body.effective_at,
            reason=body.reason,
        )
        return EnrollmentOut(
            id=e.id,
            participant_id=e.participant_id,
            batch_id=e.batch_id,
            application_id=e.application_id,
            status=e.status.value,
            effective_from=e.effective_from,
            effective_until=e.effective_until,
        )


@router.get("/enrollments", response_model=list[EnrollmentOut])
async def get_enrollments(
    batch_id: UUID | None = None,
    participant_id: UUID | None = None,
    status_filter: EnrollmentStatus | None = Query(default=None, alias="status"),
    limit: int = Query(default=50, ge=1, le=200),
    ctx: TenantContext = Depends(require_tenant_context),
) -> list[EnrollmentOut]:
    async with session_scope() as session:
        repo = EnrollmentRepository(session, ctx.tenant_id)
        rows = await repo.list(
            batch_id=batch_id,
            participant_id=participant_id,
            status=status_filter,
            limit=limit,
        )
        return [
            EnrollmentOut(
                id=e.id,
                participant_id=e.participant_id,
                batch_id=e.batch_id,
                application_id=e.application_id,
                status=e.status.value,
                effective_from=e.effective_from,
                effective_until=e.effective_until,
            )
            for e in rows
        ]


# --- TeachingAssignment endpoints --------------------------------------------


@router.post(
    "/batches/{batch_id}/assignments",
    response_model=TeachingAssignmentOut,
    status_code=status.HTTP_201_CREATED,
)
async def post_teaching_assignment(
    batch_id: UUID,
    body: AssignTeacherBody,
    ctx: TenantContext = Depends(require_tenant_context),
) -> TeachingAssignmentOut:
    async with session_scope() as session:
        a = await assign_teacher(
            session,
            ctx,
            unit_id=body.unit_id,
            membership_id=body.membership_id,
            assignment_kind=body.assignment_kind,
            batch_id=body.batch_id or batch_id,
            session_id=body.session_id,
            component_codes=body.component_codes,
            valid_from=body.valid_from,
            valid_until=body.valid_until,
        )
        return TeachingAssignmentOut(
            id=a.id,
            membership_id=a.membership_id,
            batch_id=a.batch_id,
            session_id=a.session_id,
            assignment_kind=a.assignment_kind.value,
            valid_from=a.valid_from,
            valid_until=a.valid_until,
            status=a.status.value,
        )


@router.post(
    "/batches/{batch_id}/assignments/{aid}/end",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def post_end_teaching_assignment(
    batch_id: UUID,
    aid: UUID,
    ctx: TenantContext = Depends(require_tenant_context),
) -> None:
    async with session_scope() as session:
        await end_teaching_assignment(session, ctx, aid)