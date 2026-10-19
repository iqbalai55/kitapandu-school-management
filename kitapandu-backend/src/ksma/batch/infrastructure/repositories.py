"""Batch repositories — tenant-scoped via RLS + explicit filter."""
from __future__ import annotations

from typing import Any
from uuid import UUID

from sqlalchemy import text
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


def _json(d: Any) -> str:
    import json

    return json.dumps(d, default=str)


# ---------------------------------------------------------------------------
# Batch
# ---------------------------------------------------------------------------


class BatchRepository:
    def __init__(self, session: AsyncSession, tenant_id: UUID) -> None:
        self.session = session
        self.tenant_id = tenant_id

    async def create(self, b: Batch) -> None:
        await self.session.execute(
            text(
                """
                INSERT INTO batch
                  (id, tenant_id, unit_id, batch_code, program_version_id, period_id,
                   capacity, delivery_mode, service_from, service_until, status,
                   created_at, created_by, updated_at, updated_by)
                VALUES
                  (:id, :tid, :uid, :code, :pvid, :pid2, :cap, :mode,
                   :sf, :su, :status, now(), :cb, now(), :ub)
                """
            ),
            {
                "id": str(b.id),
                "tid": str(self.tenant_id),
                "uid": str(b.unit_id),
                "code": b.batch_code,
                "pvid": str(b.program_version_id),
                "pid2": str(b.period_id) if b.period_id else None,
                "cap": b.capacity,
                "mode": b.delivery_mode.value,
                "sf": b.service_from,
                "su": b.service_until,
                "status": b.status.value,
                "cb": b.created_by,
                "ub": b.updated_by,
            },
        )

    async def get(self, id: UUID) -> Batch | None:
        row = (
            await self.session.execute(
                text(
                    """
                    SELECT id, tenant_id, unit_id, batch_code, program_version_id,
                           period_id, capacity, delivery_mode, service_from, service_until,
                           status, created_at, created_by, updated_at, updated_by
                    FROM batch WHERE id = :id AND tenant_id = :tid
                    """
                ),
                {"id": str(id), "tid": str(self.tenant_id)},
            )
        ).first()
        return _to_batch(row) if row else None

    async def lock_for_update(self, id: UUID) -> Batch | None:
        """SELECT … FOR UPDATE — used by capacity race + capacity-reduction flows."""
        row = (
            await self.session.execute(
                text(
                    """
                    SELECT id, tenant_id, unit_id, batch_code, program_version_id,
                           period_id, capacity, delivery_mode, service_from, service_until,
                           status, created_at, created_by, updated_at, updated_by
                    FROM batch WHERE id = :id AND tenant_id = :tid
                    FOR UPDATE
                    """
                ),
                {"id": str(id), "tid": str(self.tenant_id)},
            )
        ).first()
        return _to_batch(row) if row else None

    async def list(
        self,
        *,
        unit_id: UUID | None = None,
        status: BatchStatus | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[Batch]:
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
                    SELECT id, tenant_id, unit_id, batch_code, program_version_id,
                           period_id, capacity, delivery_mode, service_from, service_until,
                           status, created_at, created_by, updated_at, updated_by
                    FROM batch WHERE {' AND '.join(where)}
                    ORDER BY created_at DESC LIMIT :limit OFFSET :offset
                    """
                ),
                params,
            )
        ).all()
        return [_to_batch(r) for r in rows]

    async def set_status(self, id: UUID, *, status: BatchStatus, actor: str) -> None:
        await self.session.execute(
            text(
                "UPDATE batch SET status=:s, updated_at=now(), updated_by=:u "
                "WHERE id=:id AND tenant_id=:tid"
            ),
            {"id": str(id), "tid": str(self.tenant_id), "s": status.value, "u": actor},
        )

    async def set_capacity(self, id: UUID, *, capacity: int, actor: str) -> None:
        await self.session.execute(
            text(
                "UPDATE batch SET capacity=:cap, updated_at=now(), updated_by=:u "
                "WHERE id=:id AND tenant_id=:tid"
            ),
            {"id": str(id), "tid": str(self.tenant_id), "cap": capacity, "u": actor},
        )

    async def count_active_enrollments(self, batch_id: UUID, *, at: Any | None = None) -> int:
        """Count enrollments whose ``effective_range`` covers ``at`` (default now)."""
        if at is None:
            at = "now()"
        else:
            at = f"'{at.isoformat()}'::timestamptz"
        row = (
            await self.session.execute(
                text(
                    f"""
                    SELECT count(*)::int AS n FROM enrollment
                    WHERE tenant_id = :tid AND batch_id = :bid
                      AND status IN ('planned','active')
                      AND effective_range @> {at}
                    """
                ),
                {"tid": str(self.tenant_id), "bid": str(batch_id)},
            )
        ).first()
        return int(row.n or 0)


# ---------------------------------------------------------------------------
# Application
# ---------------------------------------------------------------------------


class ApplicationRepository:
    def __init__(self, session: AsyncSession, tenant_id: UUID) -> None:
        self.session = session
        self.tenant_id = tenant_id

    async def create(self, a: Application) -> None:
        await self.session.execute(
            text(
                """
                INSERT INTO application
                  (id, tenant_id, unit_id, participant_id, program_id, requested_start,
                   completeness, status, decision, source_ref,
                   created_at, created_by, updated_at, updated_by)
                VALUES
                  (:id, :tid, :uid, :pid, :pgid, :rs, CAST(:comp AS JSONB),
                   :status, CAST(:dec AS JSONB), :src,
                   now(), :cb, now(), :ub)
                """
            ),
            {
                "id": str(a.id),
                "tid": str(self.tenant_id),
                "uid": str(a.unit_id),
                "pid": str(a.participant_id),
                "pgid": str(a.program_id),
                "rs": a.requested_start,
                "comp": _json(a.completeness.to_json()),
                "status": a.status.value,
                "dec": _json(a.decision) if a.decision else None,
                "src": a.source_ref,
                "cb": a.created_by,
                "ub": a.updated_by,
            },
        )

    async def get(self, id: UUID) -> Application | None:
        row = (
            await self.session.execute(
                text(
                    """
                    SELECT id, tenant_id, unit_id, participant_id, program_id,
                           requested_start, completeness, status, decision, source_ref,
                           created_at, created_by, updated_at, updated_by
                    FROM application WHERE id = :id AND tenant_id = :tid
                    """
                ),
                {"id": str(id), "tid": str(self.tenant_id)},
            )
        ).first()
        return _to_application(row) if row else None

    async def list(
        self,
        *,
        status: ApplicationStatus | None = None,
        participant_id: UUID | None = None,
        unit_id: UUID | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[Application]:
        params: dict[str, Any] = {"tid": str(self.tenant_id), "limit": limit, "offset": offset}
        where = ["tenant_id = :tid"]
        if status is not None:
            where.append("status = :status")
            params["status"] = status.value
        if participant_id is not None:
            where.append("participant_id = :pid")
            params["pid"] = str(participant_id)
        if unit_id is not None:
            where.append("unit_id = :uid")
            params["uid"] = str(unit_id)
        rows = (
            await self.session.execute(
                text(
                    f"""
                    SELECT id, tenant_id, unit_id, participant_id, program_id,
                           requested_start, completeness, status, decision, source_ref,
                           created_at, created_by, updated_at, updated_by
                    FROM application WHERE {' AND '.join(where)}
                    ORDER BY created_at DESC LIMIT :limit OFFSET :offset
                    """
                ),
                params,
            )
        ).all()
        return [_to_application(r) for r in rows]

    async def update_status(
        self, id: UUID, *, status: ApplicationStatus, decision: dict[str, Any] | None, actor: str
    ) -> None:
        await self.session.execute(
            text(
                "UPDATE application SET status=:s, decision=CAST(:d AS JSONB), "
                "updated_at=now(), updated_by=:u "
                "WHERE id=:id AND tenant_id=:tid"
            ),
            {"id": str(id), "tid": str(self.tenant_id), "s": status.value, "d": _json(decision) if decision else None, "u": actor},
        )

    async def update_completeness(self, id: UUID, *, completeness: Completeness, actor: str) -> None:
        await self.session.execute(
            text(
                "UPDATE application SET completeness=CAST(:c AS JSONB), "
                "updated_at=now(), updated_by=:u "
                "WHERE id=:id AND tenant_id=:tid"
            ),
            {"id": str(id), "tid": str(self.tenant_id), "c": _json(completeness.to_json()), "u": actor},
        )


# ---------------------------------------------------------------------------
# Enrollment
# ---------------------------------------------------------------------------


class EnrollmentRepository:
    def __init__(self, session: AsyncSession, tenant_id: UUID) -> None:
        self.session = session
        self.tenant_id = tenant_id

    async def create(self, e: Enrollment) -> None:
        await self.session.execute(
            text(
                """
                INSERT INTO enrollment
                  (id, tenant_id, unit_id, participant_id, batch_id, application_id,
                   effective_range, status, policy_id,
                   created_at, created_by, updated_at, updated_by)
                VALUES
                  (:id, :tid, :uid, :pid, :bid, :appid,
                   tstzrange(:ef, :eu, '[)'),
                   :status, :pol,
                   now(), :cb, now(), :ub)
                """
            ),
            {
                "id": str(e.id),
                "tid": str(self.tenant_id),
                "uid": str(e.unit_id),
                "pid": str(e.participant_id),
                "bid": str(e.batch_id),
                "appid": str(e.application_id) if e.application_id else None,
                "ef": e.effective_from,
                "eu": e.effective_until,
                "status": e.status.value,
                "pol": str(e.policy_id) if e.policy_id else None,
                "cb": e.created_by,
                "ub": e.updated_by,
            },
        )

    async def get(self, id: UUID) -> Enrollment | None:
        row = (
            await self.session.execute(
                text(
                    """
                    SELECT id, tenant_id, unit_id, participant_id, batch_id, application_id,
                           lower(effective_range) AS effective_from,
                           upper(effective_range) AS effective_until,
                           status, policy_id,
                           created_at, created_by, updated_at, updated_by
                    FROM enrollment WHERE id = :id AND tenant_id = :tid
                    """
                ),
                {"id": str(id), "tid": str(self.tenant_id)},
            )
        ).first()
        return _to_enrollment(row) if row else None

    async def update_status(
        self,
        id: UUID,
        *,
        status: EnrollmentStatus,
        effective_until: Any | None = None,
        actor: str,
    ) -> None:
        if effective_until is None:
            await self.session.execute(
                text(
                    "UPDATE enrollment SET status=:s, updated_at=now(), updated_by=:u "
                    "WHERE id=:id AND tenant_id=:tid"
                ),
                {"id": str(id), "tid": str(self.tenant_id), "s": status.value, "u": actor},
            )
        else:
            await self.session.execute(
                text(
                    "UPDATE enrollment SET status=:s, "
                    "effective_range = tstzrange(lower(effective_range), :eu, '[)'), "
                    "updated_at=now(), updated_by=:u "
                    "WHERE id=:id AND tenant_id=:tid"
                ),
                {"id": str(id), "tid": str(self.tenant_id), "s": status.value, "eu": effective_until, "u": actor},
            )

    async def list(
        self,
        *,
        batch_id: UUID | None = None,
        participant_id: UUID | None = None,
        status: EnrollmentStatus | None = None,
        limit: int = 50,
    ) -> list[Enrollment]:
        params: dict[str, Any] = {"tid": str(self.tenant_id), "limit": limit}
        where = ["tenant_id = :tid"]
        if batch_id is not None:
            where.append("batch_id = :bid")
            params["bid"] = str(batch_id)
        if participant_id is not None:
            where.append("participant_id = :pid")
            params["pid"] = str(participant_id)
        if status is not None:
            where.append("status = :status")
            params["status"] = status.value
        rows = (
            await self.session.execute(
                text(
                    f"""
                    SELECT id, tenant_id, unit_id, participant_id, batch_id, application_id,
                           lower(effective_range) AS effective_from,
                           upper(effective_range) AS effective_until,
                           status, policy_id,
                           created_at, created_by, updated_at, updated_by
                    FROM enrollment WHERE {' AND '.join(where)}
                    ORDER BY created_at DESC LIMIT :limit
                    """
                ),
                params,
            )
        ).all()
        return [_to_enrollment(r) for r in rows]


# ---------------------------------------------------------------------------
# EnrollmentChange
# ---------------------------------------------------------------------------


class EnrollmentChangeRepository:
    def __init__(self, session: AsyncSession, tenant_id: UUID) -> None:
        self.session = session
        self.tenant_id = tenant_id

    async def create(self, c: EnrollmentChange) -> None:
        await self.session.execute(
            text(
                """
                INSERT INTO enrollment_change
                  (id, tenant_id, source_enrollment_id, target_enrollment_id,
                   change_kind, effective_at, decision, status,
                   created_at, created_by, updated_at, updated_by)
                VALUES
                  (:id, :tid, :src, :tgt, :kind, :ea, CAST(:dec AS JSONB), :status,
                   now(), :cb, now(), :ub)
                """
            ),
            {
                "id": str(c.id),
                "tid": str(self.tenant_id),
                "src": str(c.source_enrollment_id),
                "tgt": str(c.target_enrollment_id) if c.target_enrollment_id else None,
                "kind": c.change_kind.value,
                "ea": c.effective_at,
                "dec": _json(c.decision),
                "status": c.status.value,
                "cb": c.created_by,
                "ub": c.updated_by,
            },
        )


# ---------------------------------------------------------------------------
# TeachingAssignment
# ---------------------------------------------------------------------------


class TeachingAssignmentRepository:
    def __init__(self, session: AsyncSession, tenant_id: UUID) -> None:
        self.session = session
        self.tenant_id = tenant_id

    async def create(self, a: TeachingAssignment) -> None:
        await self.session.execute(
            text(
                """
                INSERT INTO teaching_assignment
                  (id, tenant_id, unit_id, membership_id, batch_id, session_id,
                   assignment_kind, component_codes, valid_from, valid_until, status,
                   created_at, created_by, updated_at, updated_by)
                VALUES
                  (:id, :tid, :uid, :mid, :bid, :sid, :kind,
                   CAST(:cc AS text[]), :vf, :vu, :status,
                   now(), :cb, now(), :ub)
                """
            ),
            {
                "id": str(a.id),
                "tid": str(self.tenant_id),
                "uid": str(a.unit_id),
                "mid": str(a.membership_id),
                "bid": str(a.batch_id) if a.batch_id else None,
                "sid": str(a.session_id) if a.session_id else None,
                "kind": a.assignment_kind.value,
                "cc": list(a.component_codes),
                "vf": a.valid_from,
                "vu": a.valid_until,
                "status": a.status.value,
                "cb": a.created_by,
                "ub": a.updated_by,
            },
        )

    async def update_status(
        self, id: UUID, *, status: AssignmentStatus, actor: str
    ) -> None:
        await self.session.execute(
            text(
                "UPDATE teaching_assignment SET status=:s, updated_at=now(), updated_by=:u "
                "WHERE id=:id AND tenant_id=:tid"
            ),
            {"id": str(id), "tid": str(self.tenant_id), "s": status.value, "u": actor},
        )


# ---------------------------------------------------------------------------
# Mapping
# ---------------------------------------------------------------------------


def _to_batch(row: Any) -> Batch:
    return Batch(
        id=row.id,
        tenant_id=row.tenant_id,
        unit_id=row.unit_id,
        batch_code=row.batch_code,
        program_version_id=row.program_version_id,
        period_id=row.period_id,
        capacity=row.capacity,
        delivery_mode=DeliveryMode(row.delivery_mode),
        service_from=row.service_from,
        service_until=row.service_until,
        status=BatchStatus(row.status),
        created_at=row.created_at,
        created_by=row.created_by,
        updated_at=row.updated_at,
        updated_by=row.updated_by,
    )


def _to_application(row: Any) -> Application:
    return Application(
        id=row.id,
        tenant_id=row.tenant_id,
        unit_id=row.unit_id,
        participant_id=row.participant_id,
        program_id=row.program_id,
        requested_start=row.requested_start,
        completeness=Completeness.from_json(row.completeness or []),
        status=ApplicationStatus(row.status),
        decision=row.decision,
        source_ref=row.source_ref,
        created_at=row.created_at,
        created_by=row.created_by,
        updated_at=row.updated_at,
        updated_by=row.updated_by,
    )


def _to_enrollment(row: Any) -> Enrollment:
    return Enrollment(
        id=row.id,
        tenant_id=row.tenant_id,
        unit_id=row.unit_id,
        participant_id=row.participant_id,
        batch_id=row.batch_id,
        application_id=row.application_id,
        effective_from=row.effective_from,
        effective_until=row.effective_until,
        status=EnrollmentStatus(row.status),
        policy_id=row.policy_id,
        created_at=row.created_at,
        created_by=row.created_by,
        updated_at=row.updated_at,
        updated_by=row.updated_by,
    )


def _to_teaching_assignment(row: Any) -> TeachingAssignment:
    return TeachingAssignment(
        id=row.id,
        tenant_id=row.tenant_id,
        unit_id=row.unit_id,
        membership_id=row.membership_id,
        batch_id=row.batch_id,
        session_id=row.session_id,
        assignment_kind=AssignmentKind(row.assignment_kind),
        component_codes=list(row.component_codes or []),
        valid_from=row.valid_from,
        valid_until=row.valid_until,
        status=AssignmentStatus(row.status),
        created_at=row.created_at,
        created_by=row.created_by,
        updated_at=row.updated_at,
        updated_by=row.updated_by,
    )