"""Program repositories — tenant-scoped via RLS + explicit filter."""
from __future__ import annotations

from typing import Any
from uuid import UUID

from sqlalchemy import text
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


def _json(d: Any) -> str:
    import json

    return json.dumps(d, default=str)


class ProgramRepository:
    def __init__(self, session: AsyncSession, tenant_id: UUID) -> None:
        self.session = session
        self.tenant_id = tenant_id

    async def create(self, p: Program) -> None:
        await self.session.execute(
            text(
                """
                INSERT INTO program
                  (id, tenant_id, unit_id, program_code, name, service_group,
                   status, current_version_id, created_at, created_by,
                   updated_at, updated_by)
                VALUES
                  (:id, :tid, :uid, :code, :name, :sg, :status, NULL,
                   now(), :cb, now(), :ub)
                """
            ),
            {
                "id": str(p.id),
                "tid": str(self.tenant_id),
                "uid": str(p.unit_id),
                "code": p.program_code,
                "name": p.name,
                "sg": p.service_group,
                "status": p.status.value,
                "cb": p.created_by,
                "ub": p.updated_by,
            },
        )

    async def get(self, id: UUID) -> Program | None:
        row = (
            await self.session.execute(
                text(
                    """
                    SELECT id, tenant_id, unit_id, program_code, name,
                           service_group, status, current_version_id,
                           created_at, created_by, updated_at, updated_by
                    FROM program
                    WHERE id = :id AND tenant_id = :tid
                    """
                ),
                {"id": str(id), "tid": str(self.tenant_id)},
            )
        ).first()
        return _to_program(row) if row else None

    async def list(
        self,
        *,
        unit_id: UUID | None = None,
        status: ProgramStatus | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[Program]:
        params: dict[str, Any] = {"tid": str(self.tenant_id), "limit": limit, "offset": offset}
        where = ["tenant_id = :tid"]
        if unit_id is not None:
            where.append("unit_id = :uid")
            params["uid"] = str(unit_id)
        if status is not None:
            where.append("status = :status")
            params["status"] = status.value
        rows = (
            await self.session.execute(
                text(
                    f"""
                    SELECT id, tenant_id, unit_id, program_code, name,
                           service_group, status, current_version_id,
                           created_at, created_by, updated_at, updated_by
                    FROM program
                    WHERE {' AND '.join(where)}
                    ORDER BY created_at DESC
                    LIMIT :limit OFFSET :offset
                    """
                ),
                params,
            )
        ).all()
        return [_to_program(r) for r in rows]

    async def set_status(self, id: UUID, *, status: ProgramStatus, actor: str) -> None:
        await self.session.execute(
            text(
                "UPDATE program SET status=:s, updated_at=now(), updated_by=:u "
                "WHERE id=:id AND tenant_id=:tid"
            ),
            {"id": str(id), "tid": str(self.tenant_id), "s": status.value, "u": actor},
        )

    async def set_current_version(self, id: UUID, version_id: UUID, actor: str) -> None:
        await self.session.execute(
            text(
                "UPDATE program SET current_version_id=:v, updated_at=now(), updated_by=:u "
                "WHERE id=:id AND tenant_id=:tid"
            ),
            {"id": str(id), "tid": str(self.tenant_id), "v": str(version_id), "u": actor},
        )


class ProgramVersionRepository:
    def __init__(self, session: AsyncSession, tenant_id: UUID) -> None:
        self.session = session
        self.tenant_id = tenant_id

    async def create(self, v: ProgramVersion) -> None:
        await self.session.execute(
            text(
                """
                INSERT INTO program_version
                  (id, tenant_id, unit_id, program_id, version_no,
                   learning_objectives, curriculum, effective_from, effective_until,
                   status, created_at, created_by, updated_at, updated_by)
                VALUES
                  (:id, :tid, :uid, :pid, :vn, :lo, CAST(:cu AS JSONB),
                   :ef, :eu, :status, now(), :cb, now(), :ub)
                """
            ),
            {
                "id": str(v.id),
                "tid": str(self.tenant_id),
                "uid": str(v.unit_id),
                "pid": str(v.program_id),
                "vn": v.version_no,
                "lo": v.learning_objectives,
                "cu": _json(v.curriculum.to_json()),
                "ef": v.effective_from,
                "eu": v.effective_until,
                "status": v.status.value,
                "cb": v.created_by,
                "ub": v.updated_by,
            },
        )

    async def get(self, id: UUID) -> ProgramVersion | None:
        row = (
            await self.session.execute(
                text(
                    """
                    SELECT id, tenant_id, unit_id, program_id, version_no,
                           learning_objectives, curriculum, effective_from, effective_until,
                           status, created_at, created_by, updated_at, updated_by
                    FROM program_version
                    WHERE id = :id AND tenant_id = :tid
                    """
                ),
                {"id": str(id), "tid": str(self.tenant_id)},
            )
        ).first()
        return _to_version(row) if row else None

    async def list_for_program(self, program_id: UUID) -> list[ProgramVersion]:
        rows = (
            await self.session.execute(
                text(
                    """
                    SELECT id, tenant_id, unit_id, program_id, version_no,
                           learning_objectives, curriculum, effective_from, effective_until,
                           status, created_at, created_by, updated_at, updated_by
                    FROM program_version
                    WHERE tenant_id = :tid AND program_id = :pid
                    ORDER BY version_no DESC
                    """
                ),
                {"tid": str(self.tenant_id), "pid": str(program_id)},
            )
        ).all()
        return [_to_version(r) for r in rows]

    async def set_status(self, id: UUID, *, status: ProgramVersionStatus, actor: str) -> None:
        await self.session.execute(
            text(
                "UPDATE program_version SET status=:s, updated_at=now(), updated_by=:u "
                "WHERE id=:id AND tenant_id=:tid"
            ),
            {"id": str(id), "tid": str(self.tenant_id), "s": status.value, "u": actor},
        )

    async def next_version_no(self, program_id: UUID) -> int:
        row = (
            await self.session.execute(
                text(
                    "SELECT COALESCE(MAX(version_no), 0) AS n FROM program_version "
                    "WHERE program_id = :pid AND tenant_id = :tid"
                ),
                {"pid": str(program_id), "tid": str(self.tenant_id)},
            )
        ).first()
        return (row.n or 0) + 1


class AcademicPeriodRepository:
    def __init__(self, session: AsyncSession, tenant_id: UUID) -> None:
        self.session = session
        self.tenant_id = tenant_id

    async def create(self, p: AcademicPeriod) -> None:
        await self.session.execute(
            text(
                """
                INSERT INTO academic_period
                  (id, tenant_id, unit_id, period_code, name, period_kind,
                   start_date, end_date, status, created_at, created_by,
                   updated_at, updated_by)
                VALUES
                  (:id, :tid, :uid, :code, :name, :kind, :sd, :ed, :status,
                   now(), :cb, now(), :ub)
                """
            ),
            {
                "id": str(p.id),
                "tid": str(self.tenant_id),
                "uid": str(p.unit_id),
                "code": p.period_code,
                "name": p.name,
                "kind": p.period_kind.value,
                "sd": p.start_date,
                "ed": p.end_date,
                "status": p.status.value,
                "cb": p.created_by,
                "ub": p.updated_by,
            },
        )

    async def get(self, id: UUID) -> AcademicPeriod | None:
        row = (
            await self.session.execute(
                text(
                    """
                    SELECT id, tenant_id, unit_id, period_code, name, period_kind,
                           start_date, end_date, status, created_at, created_by,
                           updated_at, updated_by
                    FROM academic_period
                    WHERE id = :id AND tenant_id = :tid
                    """
                ),
                {"id": str(id), "tid": str(self.tenant_id)},
            )
        ).first()
        return _to_period(row) if row else None

    async def list(
        self,
        *,
        unit_id: UUID | None = None,
        status: PeriodStatus | None = None,
    ) -> list[AcademicPeriod]:
        params: dict[str, Any] = {"tid": str(self.tenant_id)}
        where = ["tenant_id = :tid"]
        if unit_id is not None:
            where.append("unit_id = :uid")
            params["uid"] = str(unit_id)
        if status is not None:
            where.append("status = :status")
            params["status"] = status.value
        rows = (
            await self.session.execute(
                text(
                    f"""
                    SELECT id, tenant_id, unit_id, period_code, name, period_kind,
                           start_date, end_date, status, created_at, created_by,
                           updated_at, updated_by
                    FROM academic_period
                    WHERE {' AND '.join(where)}
                    ORDER BY start_date DESC
                    """
                ),
                params,
            )
        ).all()
        return [_to_period(r) for r in rows]

    async def set_status(self, id: UUID, *, status: PeriodStatus, actor: str) -> None:
        await self.session.execute(
            text(
                "UPDATE academic_period SET status=:s, updated_at=now(), updated_by=:u "
                "WHERE id=:id AND tenant_id=:tid"
            ),
            {"id": str(id), "tid": str(self.tenant_id), "s": status.value, "u": actor},
        )


class RubricRepository:
    def __init__(self, session: AsyncSession, tenant_id: UUID) -> None:
        self.session = session
        self.tenant_id = tenant_id

    async def create_version(self, v: RubricVersion) -> None:
        await self.session.execute(
            text(
                """
                INSERT INTO rubric_version
                  (id, tenant_id, unit_id, program_version_id, version_no,
                   name, status, published_at, approval, created_at, created_by,
                   updated_at, updated_by)
                VALUES
                  (:id, :tid, :uid, :pvid, :vn, :name, :status, NULL,
                   CAST(:approval AS JSONB), now(), :cb, now(), :ub)
                """
            ),
            {
                "id": str(v.id),
                "tid": str(self.tenant_id),
                "uid": str(v.unit_id),
                "pvid": str(v.program_version_id),
                "vn": v.version_no,
                "name": v.name,
                "status": v.status.value,
                "approval": _json(v.approval) if v.approval else None,
                "cb": v.created_by,
                "ub": v.updated_by,
            },
        )

    async def get_version(self, id: UUID) -> RubricVersion | None:
        row = (
            await self.session.execute(
                text(
                    """
                    SELECT id, tenant_id, unit_id, program_version_id, version_no,
                           name, status, published_at, approval,
                           created_at, created_by, updated_at, updated_by
                    FROM rubric_version
                    WHERE id = :id AND tenant_id = :tid
                    """
                ),
                {"id": str(id), "tid": str(self.tenant_id)},
            )
        ).first()
        return _to_rubric(row) if row else None

    async def list_versions(self, program_version_id: UUID) -> list[RubricVersion]:
        rows = (
            await self.session.execute(
                text(
                    """
                    SELECT id, tenant_id, unit_id, program_version_id, version_no,
                           name, status, published_at, approval,
                           created_at, created_by, updated_at, updated_by
                    FROM rubric_version
                    WHERE tenant_id = :tid AND program_version_id = :pvid
                    ORDER BY version_no DESC
                    """
                ),
                {"tid": str(self.tenant_id), "pvid": str(program_version_id)},
            )
        ).all()
        return [_to_rubric(r) for r in rows]

    async def publish_version(self, id: UUID, actor: str) -> None:
        await self.session.execute(
            text(
                "UPDATE rubric_version SET status='published', published_at=now(), "
                "updated_at=now(), updated_by=:u "
                "WHERE id=:id AND tenant_id=:tid"
            ),
            {"id": str(id), "tid": str(self.tenant_id), "u": actor},
        )

    async def next_version_no(self, program_version_id: UUID) -> int:
        row = (
            await self.session.execute(
                text(
                    "SELECT COALESCE(MAX(version_no), 0) AS n FROM rubric_version "
                    "WHERE program_version_id = :pvid AND tenant_id = :tid"
                ),
                {"pvid": str(program_version_id), "tid": str(self.tenant_id)},
            )
        ).first()
        return (row.n or 0) + 1

    async def add_indicator(self, ind: RubricIndicator) -> None:
        await self.session.execute(
            text(
                """
                INSERT INTO rubric_indicator
                  (id, tenant_id, rubric_version_id, indicator_code, label,
                   value_kind, scale, weight, is_required, created_at, created_by,
                   updated_at, updated_by)
                VALUES
                  (:id, :tid, :rvid, :code, :label, :vk, CAST(:sc AS JSONB),
                   :w, :req, now(), :cb, now(), :ub)
                """
            ),
            {
                "id": str(ind.id),
                "tid": str(self.tenant_id),
                "rvid": str(ind.rubric_version_id),
                "code": ind.indicator_code,
                "label": ind.label,
                "vk": ind.value_kind.value,
                "sc": _json(ind.scale.to_json()),
                "w": ind.weight,
                "req": ind.is_required,
                "cb": ind.created_by,
                "ub": ind.updated_by,
            },
        )

    async def list_indicators(self, rubric_version_id: UUID) -> list[RubricIndicator]:
        rows = (
            await self.session.execute(
                text(
                    """
                    SELECT id, tenant_id, rubric_version_id, indicator_code, label,
                           value_kind, scale, weight, is_required,
                           created_at, created_by, updated_at, updated_by
                    FROM rubric_indicator
                    WHERE tenant_id = :tid AND rubric_version_id = :rvid
                    ORDER BY indicator_code
                    """
                ),
                {"tid": str(self.tenant_id), "rvid": str(rubric_version_id)},
            )
        ).all()
        return [_to_indicator(r) for r in rows]


def _to_program(row: Any) -> Program:
    return Program(
        id=row.id,
        tenant_id=row.tenant_id,
        unit_id=row.unit_id,
        program_code=row.program_code,
        name=row.name,
        service_group=row.service_group,
        status=ProgramStatus(row.status),
        current_version_id=row.current_version_id,
        created_at=row.created_at,
        created_by=row.created_by,
        updated_at=row.updated_at,
        updated_by=row.updated_by,
    )


def _to_version(row: Any) -> ProgramVersion:
    return ProgramVersion(
        id=row.id,
        tenant_id=row.tenant_id,
        unit_id=row.unit_id,
        program_id=row.program_id,
        version_no=row.version_no,
        learning_objectives=row.learning_objectives,
        curriculum=Curriculum.from_json(row.curriculum or {}),
        effective_from=row.effective_from,
        effective_until=row.effective_until,
        status=ProgramVersionStatus(row.status),
        created_at=row.created_at,
        created_by=row.created_by,
        updated_at=row.updated_at,
        updated_by=row.updated_by,
    )


def _to_period(row: Any) -> AcademicPeriod:
    return AcademicPeriod(
        id=row.id,
        tenant_id=row.tenant_id,
        unit_id=row.unit_id,
        period_code=row.period_code,
        name=row.name,
        period_kind=PeriodKind(row.period_kind),
        start_date=row.start_date,
        end_date=row.end_date,
        status=PeriodStatus(row.status),
        created_at=row.created_at,
        created_by=row.created_by,
        updated_at=row.updated_at,
        updated_by=row.updated_by,
    )


def _to_rubric(row: Any) -> RubricVersion:
    return RubricVersion(
        id=row.id,
        tenant_id=row.tenant_id,
        unit_id=row.unit_id,
        program_version_id=row.program_version_id,
        version_no=row.version_no,
        name=row.name,
        status=RubricVersionStatus(row.status),
        published_at=row.published_at,
        approval=row.approval,
        created_at=row.created_at,
        created_by=row.created_by,
        updated_at=row.updated_at,
        updated_by=row.updated_by,
    )


def _to_indicator(row: Any) -> RubricIndicator:
    return RubricIndicator(
        id=row.id,
        tenant_id=row.tenant_id,
        rubric_version_id=row.rubric_version_id,
        indicator_code=row.indicator_code,
        label=row.label,
        value_kind=IndicatorValueKind(row.value_kind),
        scale=Scale.from_json(row.scale or {}),
        weight=float(row.weight) if row.weight is not None else None,
        is_required=bool(row.is_required),
        created_at=row.created_at,
        created_by=row.created_by,
        updated_at=row.updated_at,
        updated_by=row.updated_by,
    )