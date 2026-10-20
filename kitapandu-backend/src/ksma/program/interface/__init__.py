"""Program FastAPI router."""
from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, ConfigDict, Field

from ksma.program.application import (
    activate_period,
    activate_program,
    add_rubric_indicator,
    close_period,
    create_period,
    create_program,
    create_program_version,
    create_rubric_version,
    publish_program_version,
    publish_rubric_version,
    retire_program_version,
)
from ksma.program.domain import (
    IndicatorValueKind,
    PeriodKind,
    PeriodStatus,
    ProgramStatus,
    ProgramVersionStatus,
    RubricVersionStatus,
)
from ksma.program.infrastructure.repositories import (
    AcademicPeriodRepository,
    ProgramRepository,
    ProgramVersionRepository,
    RubricRepository,
)
from ksma.shared.persistence import session_scope
from ksma.shared.tenancy import TenantContext
from ksma.shared.auth.dependencies import tenant_context_dep as require_tenant_context

router = APIRouter(prefix="/api/v1", tags=["program"])



# --- Schemas -----------------------------------------------------------------


class ProgramOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    unit_id: UUID
    program_code: str
    name: str
    service_group: str
    status: str
    current_version_id: UUID | None = None


class CreateProgramBody(BaseModel):
    unit_id: UUID
    program_code: str = Field(min_length=1, max_length=40)
    name: str = Field(min_length=1, max_length=200)
    service_group: str


class CurriculumIn(BaseModel):
    components: list[dict[str, Any]] = Field(default_factory=list)
    duration_unit: str = "hours"


class CreateProgramVersionBody(BaseModel):
    learning_objectives: str = Field(min_length=1)
    curriculum: CurriculumIn = Field(default_factory=CurriculumIn)
    effective_from: datetime | None = None
    effective_until: datetime | None = None


class ProgramVersionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    program_id: UUID
    version_no: int
    learning_objectives: str
    curriculum: dict[str, Any]
    effective_from: datetime | None = None
    effective_until: datetime | None = None
    status: str


class CreateRubricVersionBody(BaseModel):
    name: str = Field(min_length=1, max_length=200)


class ScaleIn(BaseModel):
    min: float | None = None
    max: float | None = None
    categories: list[str] = Field(default_factory=list)
    max_chars: int | None = None


class AddIndicatorBody(BaseModel):
    indicator_code: str = Field(min_length=1, max_length=40)
    label: str = Field(min_length=1, max_length=200)
    value_kind: IndicatorValueKind
    scale: ScaleIn = Field(default_factory=ScaleIn)
    is_required: bool = True
    weight: float | None = None


class RubricVersionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    program_version_id: UUID
    version_no: int
    name: str
    status: str
    published_at: datetime | None = None


class RubricIndicatorOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    indicator_code: str
    label: str
    value_kind: str
    scale: dict[str, Any]
    weight: float | None = None
    is_required: bool


class AcademicPeriodOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    unit_id: UUID
    period_code: str
    name: str
    period_kind: str
    start_date: datetime
    end_date: datetime
    status: str


class CreatePeriodBody(BaseModel):
    unit_id: UUID
    period_code: str = Field(min_length=1, max_length=40)
    name: str = Field(min_length=1, max_length=200)
    period_kind: PeriodKind
    start_date: datetime
    end_date: datetime


# --- Program endpoints -------------------------------------------------------


@router.post("/programs", response_model=ProgramOut, status_code=status.HTTP_201_CREATED)
async def post_program(
    body: CreateProgramBody,
    ctx: TenantContext = Depends(require_tenant_context),
) -> ProgramOut:
    async with session_scope() as session:
        p = await create_program(
            session,
            ctx,
            unit_id=body.unit_id,
            program_code=body.program_code,
            name=body.name,
            service_group=body.service_group,
        )
        return ProgramOut(
            id=p.id,
            unit_id=p.unit_id,
            program_code=p.program_code,
            name=p.name,
            service_group=p.service_group,
            status=p.status.value,
            current_version_id=p.current_version_id,
        )


@router.get("/programs", response_model=list[ProgramOut])
async def get_programs(
    unit_id: UUID | None = None,
    status_filter: ProgramStatus | None = Query(default=None, alias="status"),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    ctx: TenantContext = Depends(require_tenant_context),
) -> list[ProgramOut]:
    async with session_scope() as session:
        repo = ProgramRepository(session, ctx.tenant_id)
        rows = await repo.list(unit_id=unit_id, status=status_filter, limit=limit, offset=offset)
        return [
            ProgramOut(
                id=p.id,
                unit_id=p.unit_id,
                program_code=p.program_code,
                name=p.name,
                service_group=p.service_group,
                status=p.status.value,
                current_version_id=p.current_version_id,
            )
            for p in rows
        ]


@router.get("/programs/{program_id}", response_model=ProgramOut)
async def get_program(
    program_id: UUID,
    ctx: TenantContext = Depends(require_tenant_context),
) -> ProgramOut:
    async with session_scope() as session:
        repo = ProgramRepository(session, ctx.tenant_id)
        p = await repo.get(program_id)
        if p is None:
            raise HTTPException(status_code=404, detail="not found")
        return ProgramOut(
            id=p.id,
            unit_id=p.unit_id,
            program_code=p.program_code,
            name=p.name,
            service_group=p.service_group,
            status=p.status.value,
            current_version_id=p.current_version_id,
        )


@router.post("/programs/{program_id}/activate", status_code=status.HTTP_204_NO_CONTENT)
async def post_activate_program(
    program_id: UUID,
    ctx: TenantContext = Depends(require_tenant_context),
) -> None:
    async with session_scope() as session:
        await activate_program(session, ctx, program_id)


@router.post(
    "/programs/{program_id}/versions",
    response_model=ProgramVersionOut,
    status_code=status.HTTP_201_CREATED,
)
async def post_program_version(
    program_id: UUID,
    body: CreateProgramVersionBody,
    ctx: TenantContext = Depends(require_tenant_context),
) -> ProgramVersionOut:
    async with session_scope() as session:
        from ksma.program.domain import Curriculum as _Curriculum

        v = await create_program_version(
            session,
            ctx,
            program_id=program_id,
            learning_objectives=body.learning_objectives,
            curriculum=_Curriculum(
                components=list(body.curriculum.components),
                duration_unit=body.curriculum.duration_unit,
            ),
            effective_from=body.effective_from,
            effective_until=body.effective_until,
        )
        return ProgramVersionOut(
            id=v.id,
            program_id=v.program_id,
            version_no=v.version_no,
            learning_objectives=v.learning_objectives,
            curriculum=v.curriculum.to_json(),
            effective_from=v.effective_from,
            effective_until=v.effective_until,
            status=v.status.value,
        )


@router.get(
    "/programs/{program_id}/versions",
    response_model=list[ProgramVersionOut],
)
async def get_program_versions(
    program_id: UUID,
    ctx: TenantContext = Depends(require_tenant_context),
) -> list[ProgramVersionOut]:
    async with session_scope() as session:
        repo = ProgramVersionRepository(session, ctx.tenant_id)
        rows = await repo.list_for_program(program_id)
        return [
            ProgramVersionOut(
                id=v.id,
                program_id=v.program_id,
                version_no=v.version_no,
                learning_objectives=v.learning_objectives,
                curriculum=v.curriculum.to_json(),
                effective_from=v.effective_from,
                effective_until=v.effective_until,
                status=v.status.value,
            )
            for v in rows
        ]


@router.post(
    "/programs/{program_id}/versions/{version_id}/publish",
    response_model=ProgramVersionOut,
)
async def post_publish_program_version(
    program_id: UUID,
    version_id: UUID,
    make_current: bool = Query(default=True),
    ctx: TenantContext = Depends(require_tenant_context),
) -> ProgramVersionOut:
    async with session_scope() as session:
        await publish_program_version(session, ctx, version_id=version_id, make_current=make_current)
        v = await ProgramVersionRepository(session, ctx.tenant_id).get(version_id)
        return ProgramVersionOut(
            id=v.id,
            program_id=v.program_id,
            version_no=v.version_no,
            learning_objectives=v.learning_objectives,
            curriculum=v.curriculum.to_json(),
            effective_from=v.effective_from,
            effective_until=v.effective_until,
            status=v.status.value,
        )


@router.post(
    "/programs/{program_id}/versions/{version_id}/retire",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def post_retire_program_version(
    program_id: UUID,
    version_id: UUID,
    ctx: TenantContext = Depends(require_tenant_context),
) -> None:
    async with session_scope() as session:
        await retire_program_version(session, ctx, version_id=version_id)


# --- Rubric endpoints --------------------------------------------------------


@router.post(
    "/programs/{program_id}/versions/{version_id}/rubrics",
    response_model=RubricVersionOut,
    status_code=status.HTTP_201_CREATED,
)
async def post_rubric_version(
    program_id: UUID,
    version_id: UUID,
    body: CreateRubricVersionBody,
    ctx: TenantContext = Depends(require_tenant_context),
) -> RubricVersionOut:
    async with session_scope() as session:
        rv = await create_rubric_version(
            session,
            ctx,
            program_version_id=version_id,
            name=body.name,
        )
        return RubricVersionOut(
            id=rv.id,
            program_version_id=rv.program_version_id,
            version_no=rv.version_no,
            name=rv.name,
            status=rv.status.value,
            published_at=rv.published_at,
        )


@router.post(
    "/programs/{program_id}/versions/{version_id}/rubrics/{rv_id}/publish",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def post_publish_rubric(
    program_id: UUID,
    version_id: UUID,
    rv_id: UUID,
    ctx: TenantContext = Depends(require_tenant_context),
) -> None:
    async with session_scope() as session:
        await publish_rubric_version(session, ctx, rubric_version_id=rv_id)


@router.post(
    "/programs/{program_id}/versions/{version_id}/rubrics/{rv_id}/indicators",
    response_model=RubricIndicatorOut,
    status_code=status.HTTP_201_CREATED,
)
async def post_rubric_indicator(
    program_id: UUID,
    version_id: UUID,
    rv_id: UUID,
    body: AddIndicatorBody,
    ctx: TenantContext = Depends(require_tenant_context),
) -> RubricIndicatorOut:
    async with session_scope() as session:
        from ksma.program.domain import Scale as _Scale

        ind = await add_rubric_indicator(
            session,
            ctx,
            rubric_version_id=rv_id,
            indicator_code=body.indicator_code,
            label=body.label,
            value_kind=body.value_kind,
            scale=_Scale(
                min=body.scale.min,
                max=body.scale.max,
                categories=list(body.scale.categories),
                max_chars=body.scale.max_chars,
            ),
            is_required=body.is_required,
            weight=body.weight,
        )
        return RubricIndicatorOut(
            id=ind.id,
            indicator_code=ind.indicator_code,
            label=ind.label,
            value_kind=ind.value_kind.value,
            scale=ind.scale.to_json(),
            weight=ind.weight,
            is_required=ind.is_required,
        )


@router.get(
    "/programs/{program_id}/versions/{version_id}/rubrics/{rv_id}/indicators",
    response_model=list[RubricIndicatorOut],
)
async def get_rubric_indicators(
    program_id: UUID,
    version_id: UUID,
    rv_id: UUID,
    ctx: TenantContext = Depends(require_tenant_context),
) -> list[RubricIndicatorOut]:
    async with session_scope() as session:
        repo = RubricRepository(session, ctx.tenant_id)
        rows = await repo.list_indicators(rv_id)
        return [
            RubricIndicatorOut(
                id=r.id,
                indicator_code=r.indicator_code,
                label=r.label,
                value_kind=r.value_kind.value,
                scale=r.scale.to_json(),
                weight=r.weight,
                is_required=r.is_required,
            )
            for r in rows
        ]


# --- AcademicPeriod endpoints ------------------------------------------------


@router.post(
    "/academic-periods",
    response_model=AcademicPeriodOut,
    status_code=status.HTTP_201_CREATED,
)
async def post_period(
    body: CreatePeriodBody,
    ctx: TenantContext = Depends(require_tenant_context),
) -> AcademicPeriodOut:
    async with session_scope() as session:
        p = await create_period(
            session,
            ctx,
            unit_id=body.unit_id,
            period_code=body.period_code,
            name=body.name,
            period_kind=body.period_kind,
            start_date=body.start_date,
            end_date=body.end_date,
        )
        return AcademicPeriodOut(
            id=p.id,
            unit_id=p.unit_id,
            period_code=p.period_code,
            name=p.name,
            period_kind=p.period_kind.value,
            start_date=p.start_date,
            end_date=p.end_date,
            status=p.status.value,
        )


@router.get("/academic-periods", response_model=list[AcademicPeriodOut])
async def get_periods(
    unit_id: UUID | None = None,
    status_filter: PeriodStatus | None = Query(default=None, alias="status"),
    ctx: TenantContext = Depends(require_tenant_context),
) -> list[AcademicPeriodOut]:
    async with session_scope() as session:
        repo = AcademicPeriodRepository(session, ctx.tenant_id)
        rows = await repo.list(unit_id=unit_id, status=status_filter)
        return [
            AcademicPeriodOut(
                id=p.id,
                unit_id=p.unit_id,
                period_code=p.period_code,
                name=p.name,
                period_kind=p.period_kind.value,
                start_date=p.start_date,
                end_date=p.end_date,
                status=p.status.value,
            )
            for p in rows
        ]


@router.post(
    "/academic-periods/{period_id}/activate",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def post_activate_period(
    period_id: UUID,
    ctx: TenantContext = Depends(require_tenant_context),
) -> None:
    async with session_scope() as session:
        await activate_period(session, ctx, period_id)


@router.post(
    "/academic-periods/{period_id}/close",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def post_close_period(
    period_id: UUID,
    ctx: TenantContext = Depends(require_tenant_context),
) -> None:
    async with session_scope() as session:
        await close_period(session, ctx, period_id)