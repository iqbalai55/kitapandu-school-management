"""Program application services."""
from __future__ import annotations

from typing import Any
from uuid import UUID, uuid4

from sqlalchemy.ext.asyncio import AsyncSession

from ksma.program.domain import (
    AcademicPeriod,
    Curriculum,
    IndicatorValueKind,
    PeriodKind,
    PeriodStatus,
    Program,
    ProgramStatus,
    ProgramVersion,
    ProgramVersionStatus,
    RubricIndicator,
    RubricVersion,
    RubricVersionStatus,
    Scale,
)
from ksma.program.domain.events import (
    AcademicPeriodActivated,
    AcademicPeriodClosed,
    AcademicPeriodCreated,
    ProgramCreated,
    ProgramStatusChanged,
    ProgramVersionCreated,
    ProgramVersionPublished,
    ProgramVersionRetired,
    RubricIndicatorAdded,
    RubricVersionCreated,
    RubricVersionPublished,
)
from ksma.program.infrastructure.repositories import (
    AcademicPeriodRepository,
    ProgramRepository,
    ProgramVersionRepository,
    RubricRepository,
)
from ksma.shared.errors import (
    DomainError,
    StateConflictError,
    ValidationError,
)
from ksma.shared.events import bus
from ksma.shared.outbox import enqueue
from ksma.shared.tenancy import TenantContext


# ---------------------------------------------------------------------------
# Program + ProgramVersion
# ---------------------------------------------------------------------------


async def create_program(
    session: AsyncSession,
    ctx: TenantContext,
    *,
    unit_id: UUID,
    program_code: str,
    name: str,
    service_group: str,
) -> Program:
    repo = ProgramRepository(session, ctx.tenant_id)
    p = Program(
        tenant_id=ctx.tenant_id,
        unit_id=unit_id,
        program_code=program_code,
        name=name,
        service_group=service_group,
        status=ProgramStatus.DRAFT,
        created_by=str(ctx.account_id),
        updated_by=str(ctx.account_id),
    )
    await repo.create(p)
    event = ProgramCreated(tenant_id=ctx.tenant_id, actor_id=ctx.account_id)
    await enqueue(session, event, aggregate_ref=f"program:{p.id}")
    bus().dispatch(event)
    return p


async def activate_program(
    session: AsyncSession, ctx: TenantContext, program_id: UUID
) -> None:
    repo = ProgramRepository(session, ctx.tenant_id)
    await repo.set_status(program_id, status=ProgramStatus.ACTIVE, actor=str(ctx.account_id))
    event = ProgramStatusChanged(tenant_id=ctx.tenant_id, actor_id=ctx.account_id)
    await enqueue(session, event, aggregate_ref=f"program:{program_id}")
    bus().dispatch(event)


async def create_program_version(
    session: AsyncSession,
    ctx: TenantContext,
    *,
    program_id: UUID,
    learning_objectives: str,
    curriculum: Curriculum,
    effective_from: Any | None = None,
    effective_until: Any | None = None,
) -> ProgramVersion:
    prepo = ProgramRepository(session, ctx.tenant_id)
    p = await prepo.get(program_id)
    if p is None:
        raise ValidationError("Program not found in this tenant")

    vrepo = ProgramVersionRepository(session, ctx.tenant_id)
    version_no = await vrepo.next_version_no(program_id)
    v = ProgramVersion(
        tenant_id=ctx.tenant_id,
        unit_id=p.unit_id,
        program_id=p.id,
        version_no=version_no,
        learning_objectives=learning_objectives,
        curriculum=curriculum,
        effective_from=effective_from,
        effective_until=effective_until,
        status=ProgramVersionStatus.DRAFT,
        created_by=str(ctx.account_id),
        updated_by=str(ctx.account_id),
    )
    await vrepo.create(v)
    event = ProgramVersionCreated(tenant_id=ctx.tenant_id, actor_id=ctx.account_id)
    await enqueue(session, event, aggregate_ref=f"program_version:{v.id}")
    bus().dispatch(event)
    return v


async def publish_program_version(
    session: AsyncSession,
    ctx: TenantContext,
    *,
    version_id: UUID,
    make_current: bool = True,
) -> None:
    vrepo = ProgramVersionRepository(session, ctx.tenant_id)
    v = await vrepo.get(version_id)
    if v is None:
        raise ValidationError("Program version not found")
    if v.status is not ProgramVersionStatus.DRAFT:
        raise StateConflictError(
            f"Program version is {v.status.value}; only `draft` can be published"
        )
    await vrepo.set_status(version_id, status=ProgramVersionStatus.PUBLISHED, actor=str(ctx.account_id))
    if make_current:
        prepo = ProgramRepository(session, ctx.tenant_id)
        await prepo.set_current_version(v.program_id, version_id, actor=str(ctx.account_id))
    event = ProgramVersionPublished(tenant_id=ctx.tenant_id, actor_id=ctx.account_id)
    await enqueue(session, event, aggregate_ref=f"program_version:{version_id}")
    bus().dispatch(event)


async def retire_program_version(
    session: AsyncSession, ctx: TenantContext, *, version_id: UUID
) -> None:
    vrepo = ProgramVersionRepository(session, ctx.tenant_id)
    v = await vrepo.get(version_id)
    if v is None:
        raise ValidationError("Program version not found")
    if v.status is not ProgramVersionStatus.PUBLISHED:
        raise StateConflictError("Only published versions can be retired")
    await vrepo.set_status(version_id, status=ProgramVersionStatus.RETIRED, actor=str(ctx.account_id))
    event = ProgramVersionRetired(tenant_id=ctx.tenant_id, actor_id=ctx.account_id)
    await enqueue(session, event, aggregate_ref=f"program_version:{version_id}")
    bus().dispatch(event)


# ---------------------------------------------------------------------------
# AcademicPeriod
# ---------------------------------------------------------------------------


async def create_period(
    session: AsyncSession,
    ctx: TenantContext,
    *,
    unit_id: UUID,
    period_code: str,
    name: str,
    period_kind: PeriodKind,
    start_date: Any,
    end_date: Any,
) -> AcademicPeriod:
    if end_date < start_date:
        raise ValidationError("end_date must be on or after start_date")
    repo = AcademicPeriodRepository(session, ctx.tenant_id)
    p = AcademicPeriod(
        tenant_id=ctx.tenant_id,
        unit_id=unit_id,
        period_code=period_code,
        name=name,
        period_kind=period_kind,
        start_date=start_date,
        end_date=end_date,
        status=PeriodStatus.PLANNED,
        created_by=str(ctx.account_id),
        updated_by=str(ctx.account_id),
    )
    await repo.create(p)
    event = AcademicPeriodCreated(tenant_id=ctx.tenant_id, actor_id=ctx.account_id)
    await enqueue(session, event, aggregate_ref=f"academic_period:{p.id}")
    bus().dispatch(event)
    return p


async def activate_period(
    session: AsyncSession, ctx: TenantContext, period_id: UUID
) -> None:
    repo = AcademicPeriodRepository(session, ctx.tenant_id)
    await repo.set_status(period_id, status=PeriodStatus.ACTIVE, actor=str(ctx.account_id))
    event = AcademicPeriodActivated(tenant_id=ctx.tenant_id, actor_id=ctx.account_id)
    await enqueue(session, event, aggregate_ref=f"academic_period:{period_id}")
    bus().dispatch(event)


async def close_period(
    session: AsyncSession, ctx: TenantContext, period_id: UUID
) -> None:
    repo = AcademicPeriodRepository(session, ctx.tenant_id)
    await repo.set_status(period_id, status=PeriodStatus.CLOSED, actor=str(ctx.account_id))
    event = AcademicPeriodClosed(tenant_id=ctx.tenant_id, actor_id=ctx.account_id)
    await enqueue(session, event, aggregate_ref=f"academic_period:{period_id}")
    bus().dispatch(event)


# ---------------------------------------------------------------------------
# RubricVersion + RubricIndicator
# ---------------------------------------------------------------------------


async def create_rubric_version(
    session: AsyncSession,
    ctx: TenantContext,
    *,
    program_version_id: UUID,
    name: str,
) -> RubricVersion:
    prepo = ProgramVersionRepository(session, ctx.tenant_id)
    pv = await prepo.get(program_version_id)
    if pv is None:
        raise ValidationError("Program version not found")
    rrepo = RubricRepository(session, ctx.tenant_id)
    version_no = await rrepo.next_version_no(program_version_id)
    rv = RubricVersion(
        tenant_id=ctx.tenant_id,
        unit_id=pv.unit_id,
        program_version_id=program_version_id,
        version_no=version_no,
        name=name,
        status=RubricVersionStatus.DRAFT,
        created_by=str(ctx.account_id),
        updated_by=str(ctx.account_id),
    )
    await rrepo.create_version(rv)
    event = RubricVersionCreated(tenant_id=ctx.tenant_id, actor_id=ctx.account_id)
    await enqueue(session, event, aggregate_ref=f"rubric_version:{rv.id}")
    bus().dispatch(event)
    return rv


async def publish_rubric_version(
    session: AsyncSession, ctx: TenantContext, *, rubric_version_id: UUID
) -> None:
    rrepo = RubricRepository(session, ctx.tenant_id)
    rv = await rrepo.get_version(rubric_version_id)
    if rv is None:
        raise ValidationError("Rubric version not found")
    if rv.status is not RubricVersionStatus.DRAFT:
        raise StateConflictError("Only draft rubric versions can be published")
    indicators = await rrepo.list_indicators(rubric_version_id)
    if not indicators:
        raise StateConflictError("Cannot publish a rubric with no indicators")
    await rrepo.publish_version(rubric_version_id, actor=str(ctx.account_id))
    event = RubricVersionPublished(tenant_id=ctx.tenant_id, actor_id=ctx.account_id)
    await enqueue(session, event, aggregate_ref=f"rubric_version:{rubric_version_id}")
    bus().dispatch(event)


async def add_rubric_indicator(
    session: AsyncSession,
    ctx: TenantContext,
    *,
    rubric_version_id: UUID,
    indicator_code: str,
    label: str,
    value_kind: IndicatorValueKind,
    scale: Scale,
    is_required: bool,
    weight: float | None = None,
) -> RubricIndicator:
    rrepo = RubricRepository(session, ctx.tenant_id)
    rv = await rrepo.get_version(rubric_version_id)
    if rv is None:
        raise ValidationError("Rubric version not found")
    if rv.status is not RubricVersionStatus.DRAFT:
        raise StateConflictError("Cannot add indicators to a published/retired rubric")
    if weight is not None and weight < 0:
        raise ValidationError("weight must be non-negative")
    ind = RubricIndicator(
        tenant_id=ctx.tenant_id,
        rubric_version_id=rubric_version_id,
        indicator_code=indicator_code,
        label=label,
        value_kind=value_kind,
        scale=scale,
        weight=weight,
        is_required=is_required,
        created_by=str(ctx.account_id),
        updated_by=str(ctx.account_id),
    )
    await rrepo.add_indicator(ind)
    event = RubricIndicatorAdded(tenant_id=ctx.tenant_id, actor_id=ctx.account_id)
    await enqueue(session, event, aggregate_ref=f"rubric_indicator:{ind.id}")
    bus().dispatch(event)
    return ind


# Re-export for the interface layer
__all__ = [
    "create_program",
    "activate_program",
    "create_program_version",
    "publish_program_version",
    "retire_program_version",
    "create_period",
    "activate_period",
    "close_period",
    "create_rubric_version",
    "publish_rubric_version",
    "add_rubric_indicator",
]