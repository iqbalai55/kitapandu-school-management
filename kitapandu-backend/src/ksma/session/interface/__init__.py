"""Session FastAPI router."""
from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, ConfigDict, Field

from ksma.session.application import (
    add_revision,
    cancel_session,
    close_session,
    correct_attendance,
    correct_session,
    create_session,
    record_attendance,
    record_substitute,
    write_journal,
)
from ksma.session.domain import (
    AttendanceCode,
    SessionLifecycleStatus,
)
from ksma.session.infrastructure.repositories import (
    AttendanceRepository,
    SessionRepository,
    SessionCompletionRepository,
    SessionRevisionRepository,
    TeachingJournalRepository,
)
from ksma.shared.persistence import session_scope
from ksma.shared.tenancy import TenantContext
from ksma.shared.auth.dependencies import tenant_context_dep as require_tenant_context

router = APIRouter(prefix="/api/v1", tags=["session"])



# --- Schemas -----------------------------------------------------------------


class CreateSessionBody(BaseModel):
    batch_id: UUID


class AddRevisionBody(BaseModel):
    starts_at: datetime
    ends_at: datetime
    timezone: str = "Asia/Jakarta"
    change_reason: str = Field(min_length=1, max_length=1000)
    location: dict[str, Any] | None = None


class SessionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    batch_id: UUID
    session_no: int
    lifecycle_status: str
    effective_revision_id: UUID | None = None
    closure_id: UUID | None = None


class SessionRevisionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    session_id: UUID
    revision_no: int
    starts_at: datetime
    ends_at: datetime
    timezone: str
    change_reason: str
    published_at: datetime | None = None


class CancelBody(BaseModel):
    reason: str = Field(min_length=1, max_length=1000)


class AttendanceBody(BaseModel):
    enrollment_id: UUID
    attendance_code: AttendanceCode
    note: str | None = Field(default=None, max_length=1000)


class AttendanceCorrectBody(BaseModel):
    enrollment_id: UUID
    attendance_code: AttendanceCode | None = None
    correction_reason: str = Field(min_length=1, max_length=1000)


class AttendanceOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    session_id: UUID
    enrollment_id: UUID
    attendance_code: str | None = None
    note: str | None = None
    recorded_at: datetime | None = None
    correction_reason: str | None = None


class JournalBody(BaseModel):
    actual_instructor_id: UUID
    topic: str = Field(min_length=1)
    activities: str | None = None
    internal_note: str | None = None
    portal_summary: str | None = None
    follow_up: str | None = None


class JournalOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    session_id: UUID
    actual_instructor_id: UUID
    topic: str
    activities: str | None = None
    portal_summary: str | None = None
    follow_up: str | None = None


class CorrectSessionBody(BaseModel):
    correction_reason: str = Field(min_length=1, max_length=1000)
    attendance_changes: list[dict[str, Any]] = Field(default_factory=list)


class CompletionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    session_id: UUID
    completion_no: int
    outcome: str
    supersedes_id: UUID | None = None
    correction_reason: str | None = None


class SubstituteBody(BaseModel):
    substitute_membership_id: UUID
    handover_note: str = Field(min_length=1, max_length=2000)


# --- Session endpoints -------------------------------------------------------


@router.post("/sessions", response_model=SessionOut, status_code=status.HTTP_201_CREATED)
async def post_session(
    body: CreateSessionBody,
    ctx: TenantContext = Depends(require_tenant_context),
) -> SessionOut:
    async with session_scope() as session:
        s = await create_session(session, ctx, batch_id=body.batch_id)
        return SessionOut(
            id=s.id,
            batch_id=s.batch_id,
            session_no=s.session_no,
            lifecycle_status=s.lifecycle_status.value,
        )


@router.get("/sessions/{session_id}", response_model=SessionOut)
async def get_session(
    session_id: UUID,
    ctx: TenantContext = Depends(require_tenant_context),
) -> SessionOut:
    async with session_scope() as session:
        repo = SessionRepository(session, ctx.tenant_id)
        s = await repo.get(session_id)
        if s is None:
            raise HTTPException(status_code=404, detail="not found")
        return SessionOut(
            id=s.id,
            batch_id=s.batch_id,
            session_no=s.session_no,
            lifecycle_status=s.lifecycle_status.value,
            effective_revision_id=s.effective_revision_id,
            closure_id=s.closure_id,
        )


@router.post(
    "/sessions/{session_id}/revisions",
    response_model=SessionRevisionOut,
    status_code=status.HTTP_201_CREATED,
)
async def post_revision(
    session_id: UUID,
    body: AddRevisionBody,
    ctx: TenantContext = Depends(require_tenant_context),
) -> SessionRevisionOut:
    async with session_scope() as session:
        r = await add_revision(
            session,
            ctx,
            session_id=session_id,
            starts_at=body.starts_at,
            ends_at=body.ends_at,
            timezone_str=body.timezone,
            change_reason=body.change_reason,
            location=body.location,
            make_effective=True,
        )
        return SessionRevisionOut(
            id=r.id,
            session_id=r.session_id,
            revision_no=r.revision_no,
            starts_at=r.starts_at,
            ends_at=r.ends_at,
            timezone=r.timezone,
            change_reason=r.change_reason,
            published_at=r.published_at,
        )


@router.post(
    "/sessions/{session_id}/cancel",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def post_cancel_session(
    session_id: UUID,
    body: CancelBody,
    ctx: TenantContext = Depends(require_tenant_context),
) -> None:
    async with session_scope() as session:
        await cancel_session(session, ctx, session_id=session_id, reason=body.reason)


# --- Attendance endpoints ----------------------------------------------------


@router.post(
    "/sessions/{session_id}/attendance",
    response_model=AttendanceOut,
    status_code=status.HTTP_201_CREATED,
)
async def post_attendance(
    session_id: UUID,
    body: AttendanceBody,
    ctx: TenantContext = Depends(require_tenant_context),
) -> AttendanceOut:
    async with session_scope() as session:
        a = await record_attendance(
            session,
            ctx,
            session_id=session_id,
            enrollment_id=body.enrollment_id,
            attendance_code=body.attendance_code,
            note=body.note,
        )
        return AttendanceOut(
            id=a.id,
            session_id=a.session_id,
            enrollment_id=a.enrollment_id,
            attendance_code=a.attendance_code.value if a.attendance_code else None,
            note=a.note,
            recorded_at=a.recorded_at,
        )


@router.post(
    "/sessions/{session_id}/attendance/{enrollment_id}/correct",
    response_model=AttendanceOut,
)
async def post_attendance_correct(
    session_id: UUID,
    enrollment_id: UUID,
    body: AttendanceCorrectBody,
    ctx: TenantContext = Depends(require_tenant_context),
) -> AttendanceOut:
    async with session_scope() as session:
        a = await correct_attendance(
            session,
            ctx,
            session_id=session_id,
            enrollment_id=enrollment_id,
            attendance_code=body.attendance_code,
            correction_reason=body.correction_reason,
        )
        return AttendanceOut(
            id=a.id,
            session_id=a.session_id,
            enrollment_id=a.enrollment_id,
            attendance_code=a.attendance_code.value if a.attendance_code else None,
            note=a.note,
            recorded_at=a.recorded_at,
            correction_reason=a.correction_reason,
        )


@router.get(
    "/sessions/{session_id}/attendance",
    response_model=list[AttendanceOut],
)
async def get_attendance(
    session_id: UUID,
    ctx: TenantContext = Depends(require_tenant_context),
) -> list[AttendanceOut]:
    async with session_scope() as session:
        repo = AttendanceRepository(session, ctx.tenant_id)
        rows = await repo.list_for_session(session_id)
        return [
            AttendanceOut(
                id=a.id,
                session_id=a.session_id,
                enrollment_id=a.enrollment_id,
                attendance_code=a.attendance_code.value if a.attendance_code else None,
                note=a.note,
                recorded_at=a.recorded_at,
                correction_reason=a.correction_reason,
            )
            for a in rows
        ]


# --- Journal endpoints --------------------------------------------------------


@router.put(
    "/sessions/{session_id}/journal",
    response_model=JournalOut,
)
async def put_journal(
    session_id: UUID,
    body: JournalBody,
    ctx: TenantContext = Depends(require_tenant_context),
) -> JournalOut:
    async with session_scope() as session:
        j = await write_journal(
            session,
            ctx,
            session_id=session_id,
            actual_instructor_id=body.actual_instructor_id,
            topic=body.topic,
            activities=body.activities,
            internal_note=body.internal_note,
            portal_summary=body.portal_summary,
            follow_up=body.follow_up,
        )
        return JournalOut(
            id=j.id,
            session_id=j.session_id,
            actual_instructor_id=j.actual_instructor_id,
            topic=j.topic,
            activities=j.activities,
            portal_summary=j.portal_summary,
            follow_up=j.follow_up,
        )


@router.get(
    "/sessions/{session_id}/journal",
    response_model=JournalOut | None,
)
async def get_journal(
    session_id: UUID,
    ctx: TenantContext = Depends(require_tenant_context),
) -> JournalOut | None:
    async with session_scope() as session:
        repo = TeachingJournalRepository(session, ctx.tenant_id)
        j = await repo.get(session_id)
        if j is None:
            return None
        return JournalOut(
            id=j.id,
            session_id=j.session_id,
            actual_instructor_id=j.actual_instructor_id,
            topic=j.topic,
            activities=j.activities,
            portal_summary=j.portal_summary,
            follow_up=j.follow_up,
        )


# --- Closure + correction + substitute ---------------------------------------


@router.post(
    "/sessions/{session_id}/close",
    response_model=CompletionOut,
)
async def post_close_session(
    session_id: UUID,
    policy_id: UUID | None = None,
    ctx: TenantContext = Depends(require_tenant_context),
) -> CompletionOut:
    async with session_scope() as session:
        c = await close_session(session, ctx, session_id=session_id, policy_id=policy_id)
        return CompletionOut(
            id=c.id,
            session_id=c.session_id,
            completion_no=c.completion_no,
            outcome=c.outcome.value,
            supersedes_id=c.supersedes_id,
        )


@router.post(
    "/sessions/{session_id}/corrections",
    response_model=CompletionOut,
)
async def post_correct_session(
    session_id: UUID,
    body: CorrectSessionBody,
    ctx: TenantContext = Depends(require_tenant_context),
) -> CompletionOut:
    async with session_scope() as session:
        c = await correct_session(
            session,
            ctx,
            session_id=session_id,
            correction_reason=body.correction_reason,
            attendance_changes=body.attendance_changes,
        )
        return CompletionOut(
            id=c.id,
            session_id=c.session_id,
            completion_no=c.completion_no,
            outcome=c.outcome.value,
            supersedes_id=c.supersedes_id,
            correction_reason=c.correction_reason,
        )


@router.post(
    "/sessions/{session_id}/substitute",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def post_substitute(
    session_id: UUID,
    body: SubstituteBody,
    ctx: TenantContext = Depends(require_tenant_context),
) -> None:
    async with session_scope() as session:
        await record_substitute(
            session,
            ctx,
            session_id=session_id,
            substitute_membership_id=body.substitute_membership_id,
            handover_note=body.handover_note,
        )