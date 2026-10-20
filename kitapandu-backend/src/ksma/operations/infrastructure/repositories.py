"""Operations + portal repositories — tenant-scoped raw SQL.

Every public method on the per-table repositories takes ``(session, tenant_id)``;
RLS is enforced by ``app.current_tenant`` plus the per-table policy. The
``operational_run`` and ``background_job`` tables are not tenant-scoped, so
their repositories do not require ``tenant_id`` and instead enforce filters
explicitly in WHERE.
"""
from __future__ import annotations

import json
from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from ksma.operations.domain import (
    Attachment,
    AttachmentCheckStatus,
    ExportJob,
    ExportStatus,
    ImportBatch,
    ImportRowOutcome,
    ImportRowResult,
    ImportStatus,
    MetricDefinition,
    MetricTimeBasis,
    OperationalEnvironment,
    OperationalRun,
    OperationalRunStatus,
    OperationKind,
)


def _json(d: Any) -> str:
    return json.dumps(d, default=str)


def _tzrange_literal(lo: datetime | None, hi: datetime | None) -> str:
    """Build a Postgres ``tstzrange '[' lo, hi ')'`` literal."""
    lo_lit = "'-infinity'::timestamptz" if lo is None else f"'{lo.isoformat()}'::timestamptz"
    hi_lit = "'infinity'::timestamptz" if hi is None else f"'{hi.isoformat()}'::timestamptz"
    return f"tstzrange({lo_lit}, {hi_lit}, '[)')"


# ---------------------------------------------------------------------------
# Attachment
# ---------------------------------------------------------------------------


class AttachmentRepository:
    def __init__(self, session: AsyncSession, tenant_id: UUID) -> None:
        self.session = session
        self.tenant_id = tenant_id

    async def create(self, a: Attachment) -> None:
        await self.session.execute(
            text(
                """
                INSERT INTO attachment
                  (id, tenant_id, owner_ref, storage_key, original_name, media_type,
                   size_bytes, content_hash, check_status, retention_policy_id,
                   created_at, created_by)
                VALUES
                  (:id, :tid, CAST(:owner_ref AS JSONB), :storage_key, :orig, :media,
                   :size, :hash, :status, :rpid,
                   now(), :cb)
                """
            ),
            {
                "id": str(a.id),
                "tid": str(self.tenant_id),
                "owner_ref": _json(a.owner_ref),
                "storage_key": a.storage_key,
                "orig": a.original_name,
                "media": a.media_type,
                "size": a.size_bytes,
                "hash": a.content_hash,
                "status": a.check_status.value
                if isinstance(a.check_status, AttachmentCheckStatus)
                else a.check_status,
                "rpid": str(a.retention_policy_id) if a.retention_policy_id else None,
                "cb": a.created_by,
            },
        )

    async def get(self, id: UUID) -> Attachment | None:
        row = (
            await self.session.execute(
                text(
                    """
                    SELECT id, tenant_id, owner_ref, storage_key, original_name, media_type,
                           size_bytes, content_hash, check_status, retention_policy_id,
                           created_at, created_by
                    FROM attachment WHERE id = :id AND tenant_id = :tid
                    """
                ),
                {"id": str(id), "tid": str(self.tenant_id)},
            )
        ).first()
        return _to_attachment(row) if row else None

    async def lock_for_update(self, id: UUID) -> Attachment | None:
        row = (
            await self.session.execute(
                text(
                    """
                    SELECT id, tenant_id, owner_ref, storage_key, original_name, media_type,
                           size_bytes, content_hash, check_status, retention_policy_id,
                           created_at, created_by
                    FROM attachment WHERE id = :id AND tenant_id = :tid
                    FOR UPDATE
                    """
                ),
                {"id": str(id), "tid": str(self.tenant_id)},
            )
        ).first()
        return _to_attachment(row) if row else None

    async def set_check_status(
        self,
        id: UUID,
        *,
        check_status: AttachmentCheckStatus,
    ) -> None:
        await self.session.execute(
            text(
                """
                UPDATE attachment
                SET check_status = :s
                WHERE id = :id AND tenant_id = :tid
                """
            ),
            {"id": str(id), "tid": str(self.tenant_id), "s": check_status.value},
        )

    async def list_for_owner(
        self, owner_kind: str, owner_id: UUID, limit: int = 100
    ) -> list[Attachment]:
        rows = (
            await self.session.execute(
                text(
                    """
                    SELECT id, tenant_id, owner_ref, storage_key, original_name, media_type,
                           size_bytes, content_hash, check_status, retention_policy_id,
                           created_at, created_by
                    FROM attachment
                    WHERE tenant_id = :tid
                      AND owner_ref ->> 'kind' = :kind
                      AND owner_ref ->> 'id'   = :oid
                    ORDER BY created_at DESC
                    LIMIT :lim
                    """
                ),
                {
                    "tid": str(self.tenant_id),
                    "kind": owner_kind,
                    "oid": str(owner_id),
                    "lim": limit,
                },
            )
        ).all()
        return [_to_attachment(r) for r in rows]


# ---------------------------------------------------------------------------
# Import
# ---------------------------------------------------------------------------


class ImportRepository:
    def __init__(self, session: AsyncSession, tenant_id: UUID) -> None:
        self.session = session
        self.tenant_id = tenant_id

    async def create(self, b: ImportBatch) -> None:
        await self.session.execute(
            text(
                """
                INSERT INTO import_batch
                  (id, tenant_id, requested_by, dataset_kind, source_attachment_id,
                   mapping, scope, status, commit_request_id, counts,
                   created_at, updated_at)
                VALUES
                  (:id, :tid, :rb, :dk, :src,
                   CAST(:mapping AS JSONB), CAST(:scope AS JSONB), :status, :cri,
                   CAST(:counts AS JSONB), now(), now())
                """
            ),
            {
                "id": str(b.id),
                "tid": str(self.tenant_id),
                "rb": str(b.requested_by),
                "dk": b.dataset_kind,
                "src": str(b.source_attachment_id) if b.source_attachment_id else None,
                "mapping": _json(b.mapping),
                "scope": _json(b.scope),
                "status": b.status.value if isinstance(b.status, ImportStatus) else b.status,
                "cri": str(b.commit_request_id) if b.commit_request_id else None,
                "counts": _json(b.counts or {"valid": 0, "invalid": 0, "duplicate_candidate": 0}),
            },
        )

    async def get(self, id: UUID) -> ImportBatch | None:
        row = (
            await self.session.execute(
                text(
                    """
                    SELECT id, tenant_id, requested_by, dataset_kind, source_attachment_id,
                           mapping, scope, status, commit_request_id, counts,
                           created_at, updated_at
                    FROM import_batch WHERE id = :id AND tenant_id = :tid
                    """
                ),
                {"id": str(id), "tid": str(self.tenant_id)},
            )
        ).first()
        return _to_import_batch(row) if row else None

    async def lock_for_update(self, id: UUID) -> ImportBatch | None:
        row = (
            await self.session.execute(
                text(
                    """
                    SELECT id, tenant_id, requested_by, dataset_kind, source_attachment_id,
                           mapping, scope, status, commit_request_id, counts,
                           created_at, updated_at
                    FROM import_batch WHERE id = :id AND tenant_id = :tid
                    FOR UPDATE
                    """
                ),
                {"id": str(id), "tid": str(self.tenant_id)},
            )
        ).first()
        return _to_import_batch(row) if row else None

    async def set_status(
        self,
        id: UUID,
        *,
        status: ImportStatus,
        counts: dict[str, Any] | None = None,
        commit_request_id: UUID | None = None,
    ) -> None:
        await self.session.execute(
            text(
                """
                UPDATE import_batch
                SET status          = :s,
                    counts          = COALESCE(CAST(:counts AS JSONB), counts),
                    commit_request_id = COALESCE(:cri, commit_request_id),
                    updated_at      = now()
                WHERE id = :id AND tenant_id = :tid
                """
            ),
            {
                "id": str(id),
                "tid": str(self.tenant_id),
                "s": status.value,
                "counts": _json(counts) if counts else None,
                "cri": str(commit_request_id) if commit_request_id else None,
            },
        )

    async def find_by_commit_request_id(
        self, commit_request_id: UUID
    ) -> ImportBatch | None:
        row = (
            await self.session.execute(
                text(
                    """
                    SELECT id, tenant_id, requested_by, dataset_kind, source_attachment_id,
                           mapping, scope, status, commit_request_id, counts,
                           created_at, updated_at
                    FROM import_batch
                    WHERE commit_request_id = :cri AND tenant_id = :tid
                    ORDER BY created_at DESC LIMIT 1
                    """
                ),
                {"cri": str(commit_request_id), "tid": str(self.tenant_id)},
            )
        ).first()
        return _to_import_batch(row) if row else None

    async def upsert_row_results(
        self,
        batch_id: UUID,
        results: list[ImportRowResult],
    ) -> None:
        if not results:
            return
        for r in results:
            await self.session.execute(
                text(
                    """
                    INSERT INTO import_row_result
                      (id, tenant_id, import_batch_id, row_no, row_fingerprint, outcome,
                       errors, target_ref, source_reference)
                    VALUES
                      (:id, :tid, :bid, :rn, :fp, :oc,
                       CAST(:errors AS JSONB), CAST(:tref AS JSONB), :sr)
                    ON CONFLICT (import_batch_id, row_no) DO UPDATE SET
                      outcome = EXCLUDED.outcome,
                      errors  = EXCLUDED.errors,
                      target_ref = EXCLUDED.target_ref,
                      source_reference = EXCLUDED.source_reference
                    """
                ),
                {
                    "id": str(r.id),
                    "tid": str(self.tenant_id),
                    "bid": str(batch_id),
                    "rn": r.row_no,
                    "fp": r.row_fingerprint,
                    "oc": r.outcome.value
                    if isinstance(r.outcome, ImportRowOutcome)
                    else r.outcome,
                    "errors": _json(r.errors)
                    if r.errors
                    else None,
                    "tref": _json(r.target_ref)
                    if r.target_ref
                    else None,
                    "sr": r.source_reference,
                },
            )

    async def list_row_results(self, batch_id: UUID) -> list[ImportRowResult]:
        rows = (
            await self.session.execute(
                text(
                    """
                    SELECT id, tenant_id, import_batch_id, row_no, row_fingerprint, outcome,
                           errors, target_ref, source_reference
                    FROM import_row_result
                    WHERE import_batch_id = :bid AND tenant_id = :tid
                    ORDER BY row_no
                    """
                ),
                {"bid": str(batch_id), "tid": str(self.tenant_id)},
            )
        ).all()
        return [_to_row_result(r) for r in rows]


# ---------------------------------------------------------------------------
# Export
# ---------------------------------------------------------------------------


class ExportRepository:
    def __init__(self, session: AsyncSession, tenant_id: UUID) -> None:
        self.session = session
        self.tenant_id = tenant_id

    async def queue(self, e: ExportJob) -> None:
        await self.session.execute(
            text(
                """
                INSERT INTO export_job
                  (id, tenant_id, requester_account_id, dataset_kind, scope, filters,
                   as_of, status, result_attachment_id, expires_at,
                   created_at, updated_at)
                VALUES
                  (:id, :tid, :req, :dk, CAST(:scope AS JSONB), CAST(:filters AS JSONB),
                   :asof, :status, :result, :exp,
                   now(), now())
                """
            ),
            {
                "id": str(e.id),
                "tid": str(self.tenant_id),
                "req": str(e.requester_account_id),
                "dk": e.dataset_kind,
                "scope": _json(e.scope),
                "filters": _json(e.filters),
                "asof": e.as_of,
                "status": e.status.value if isinstance(e.status, ExportStatus) else e.status,
                "result": str(e.result_attachment_id) if e.result_attachment_id else None,
                "exp": e.expires_at,
            },
        )

    async def get(self, id: UUID) -> ExportJob | None:
        row = (
            await self.session.execute(
                text(
                    """
                    SELECT id, tenant_id, requester_account_id, dataset_kind, scope, filters,
                           as_of, status, result_attachment_id, expires_at,
                           created_at, updated_at
                    FROM export_job WHERE id = :id AND tenant_id = :tid
                    """
                ),
                {"id": str(id), "tid": str(self.tenant_id)},
            )
        ).first()
        return _to_export(row) if row else None

    async def lock_for_update(self, id: UUID) -> ExportJob | None:
        row = (
            await self.session.execute(
                text(
                    """
                    SELECT id, tenant_id, requester_account_id, dataset_kind, scope, filters,
                           as_of, status, result_attachment_id, expires_at,
                           created_at, updated_at
                    FROM export_job WHERE id = :id AND tenant_id = :tid
                    FOR UPDATE
                    """
                ),
                {"id": str(id), "tid": str(self.tenant_id)},
            )
        ).first()
        return _to_export(row) if row else None

    async def set_running(self, id: UUID) -> None:
        await self.session.execute(
            text(
                """
                UPDATE export_job
                SET status = 'running', updated_at = now()
                WHERE id = :id AND tenant_id = :tid
                """
            ),
            {"id": str(id), "tid": str(self.tenant_id)},
        )

    async def attach_result(
        self,
        id: UUID,
        *,
        result_attachment_id: UUID,
        status: ExportStatus = ExportStatus.READY,
        expires_at: datetime | None = None,
    ) -> None:
        await self.session.execute(
            text(
                """
                UPDATE export_job
                SET result_attachment_id = :aid,
                    status               = :s,
                    expires_at           = COALESCE(:exp, expires_at),
                    updated_at           = now()
                WHERE id = :id AND tenant_id = :tid
                """
            ),
            {
                "id": str(id),
                "tid": str(self.tenant_id),
                "aid": str(result_attachment_id),
                "s": status.value,
                "exp": expires_at,
            },
        )

    async def fail(self, id: UUID, *, reason: str) -> None:
        # Persist the reason on the row indirectly via a metadata column. The
        # export_job table has no dedicated failure_reason column, so we
        # stash it on the existing scope (which is a jsonb).
        await self.session.execute(
            text(
                """
                UPDATE export_job
                SET status     = 'failed',
                    scope      = scope || jsonb_build_object('failure_reason', :r::text),
                    updated_at = now()
                WHERE id = :id AND tenant_id = :tid
                """
            ),
            {"id": str(id), "tid": str(self.tenant_id), "r": reason},
        )


# ---------------------------------------------------------------------------
# MetricDefinition
# ---------------------------------------------------------------------------


class MetricRepository:
    def __init__(self, session: AsyncSession, tenant_id: UUID) -> None:
        self.session = session
        self.tenant_id = tenant_id

    async def define(self, m: MetricDefinition) -> None:
        await self.session.execute(
            text(
                f"""
                INSERT INTO metric_definition
                  (id, tenant_id, metric_code, version_no, label, source_spec, time_basis,
                   effective_range, approval, created_at)
                VALUES
                  (:id, :tid, :code, :ver, :label, CAST(:spec AS JSONB), :tb,
                   {_tzrange_literal(m.effective_from, m.effective_until)},
                   CAST(:approval AS JSONB), now())
                """
            ),
            {
                "id": str(m.id),
                "tid": str(self.tenant_id),
                "code": m.metric_code,
                "ver": m.version_no,
                "label": m.label,
                "spec": _json(m.source_spec),
                "tb": m.time_basis.value
                if isinstance(m.time_basis, MetricTimeBasis)
                else m.time_basis,
                "approval": _json(m.approval),
            },
        )

    async def get(self, id: UUID) -> MetricDefinition | None:
        row = (
            await self.session.execute(
                text(
                    """
                    SELECT id, tenant_id, metric_code, version_no, label, source_spec,
                           time_basis, lower(effective_range) AS effective_from,
                           upper(effective_range) AS effective_until,
                           approval, created_at
                    FROM metric_definition WHERE id = :id AND tenant_id = :tid
                    """
                ),
                {"id": str(id), "tid": str(self.tenant_id)},
            )
        ).first()
        return _to_metric(row) if row else None

    async def list_for_code(
        self, metric_code: str, limit: int = 50
    ) -> list[MetricDefinition]:
        rows = (
            await self.session.execute(
                text(
                    """
                    SELECT id, tenant_id, metric_code, version_no, label, source_spec,
                           time_basis, lower(effective_range) AS effective_from,
                           upper(effective_range) AS effective_until,
                           approval, created_at
                    FROM metric_definition
                    WHERE tenant_id = :tid AND metric_code = :code
                    ORDER BY version_no DESC
                    LIMIT :lim
                    """
                ),
                {"tid": str(self.tenant_id), "code": metric_code, "lim": limit},
            )
        ).all()
        return [_to_metric(r) for r in rows]


# ---------------------------------------------------------------------------
# OperationalRun (NOT tenant-scoped; explicit id filter instead)
# ---------------------------------------------------------------------------


class OperationalRunRepository:
    def __init__(self, session: AsyncSession) -> None:
        # NB: operational_run.tenant_id is nullable and the table is NOT RLS-scoped.
        self.session = session

    async def start(self, r: OperationalRun) -> None:
        await self.session.execute(
            text(
                """
                INSERT INTO operational_run
                  (id, tenant_id, operation_kind, environment, execution_subject,
                   authorization_ref, started_at, finished_at, status, evidence, created_at)
                VALUES
                  (:id, :tid, :kind, :env, :es,
                   CAST(:auth AS JSONB), :sa, :fa, :status, CAST(:ev AS JSONB), now())
                """
            ),
            {
                "id": str(r.id),
                "tid": str(r.tenant_id) if r.tenant_id else None,
                "kind": r.operation_kind.value
                if isinstance(r.operation_kind, OperationKind)
                else r.operation_kind,
                "env": r.environment.value
                if isinstance(r.environment, OperationalEnvironment)
                else r.environment,
                "es": str(r.execution_subject),
                "auth": _json(r.authorization_ref),
                "sa": r.started_at,
                "fa": r.finished_at,
                "status": r.status.value
                if isinstance(r.status, OperationalRunStatus)
                else r.status,
                "ev": _json(r.evidence) if r.evidence else None,
            },
        )

    async def get(self, id: UUID) -> OperationalRun | None:
        row = (
            await self.session.execute(
                text(
                    """
                    SELECT id, tenant_id, operation_kind, environment, execution_subject,
                           authorization_ref, started_at, finished_at, status, evidence,
                           created_at
                    FROM operational_run WHERE id = :id
                    """
                ),
                {"id": str(id)},
            )
        ).first()
        return _to_op_run(row) if row else None

    async def finish(
        self,
        id: UUID,
        *,
        status: OperationalRunStatus,
        finished_at: datetime,
        evidence: dict[str, Any] | None,
    ) -> None:
        await self.session.execute(
            text(
                """
                UPDATE operational_run
                SET status      = :s,
                    finished_at = :fa,
                    evidence    = CAST(:ev AS JSONB)
                WHERE id = :id
                """
            ),
            {
                "id": str(id),
                "s": status.value,
                "fa": finished_at,
                "ev": _json(evidence) if evidence else None,
            },
        )


# ---------------------------------------------------------------------------
# Mappers
# ---------------------------------------------------------------------------


def _to_attachment(row: Any) -> Attachment:
    return Attachment(
        id=row.id,
        tenant_id=row.tenant_id,
        owner_ref=row.owner_ref or {},
        storage_key=row.storage_key,
        original_name=row.original_name,
        media_type=row.media_type,
        size_bytes=int(row.size_bytes),
        content_hash=row.content_hash,
        check_status=AttachmentCheckStatus(row.check_status),
        retention_policy_id=row.retention_policy_id,
        created_at=row.created_at,
        created_by=row.created_by,
    )


def _to_import_batch(row: Any) -> ImportBatch:
    return ImportBatch(
        id=row.id,
        tenant_id=row.tenant_id,
        requested_by=row.requested_by,
        dataset_kind=row.dataset_kind,
        source_attachment_id=row.source_attachment_id,
        mapping=row.mapping or {},
        scope=row.scope or {},
        status=ImportStatus(row.status),
        commit_request_id=row.commit_request_id,
        counts=row.counts or {},
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def _to_row_result(row: Any) -> ImportRowResult:
    return ImportRowResult(
        id=row.id,
        tenant_id=row.tenant_id,
        import_batch_id=row.import_batch_id,
        row_no=int(row.row_no),
        row_fingerprint=row.row_fingerprint,
        outcome=ImportRowOutcome(row.outcome),
        errors=row.errors,
        target_ref=row.target_ref,
        source_reference=row.source_reference,
    )


def _to_export(row: Any) -> ExportJob:
    return ExportJob(
        id=row.id,
        tenant_id=row.tenant_id,
        requester_account_id=row.requester_account_id,
        dataset_kind=row.dataset_kind,
        scope=row.scope or {},
        filters=row.filters or {},
        as_of=row.as_of,
        status=ExportStatus(row.status),
        result_attachment_id=row.result_attachment_id,
        expires_at=row.expires_at,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def _to_metric(row: Any) -> MetricDefinition:
    return MetricDefinition(
        id=row.id,
        tenant_id=row.tenant_id,
        metric_code=row.metric_code,
        version_no=int(row.version_no),
        label=row.label,
        source_spec=row.source_spec or {},
        time_basis=MetricTimeBasis(row.time_basis),
        effective_from=row.effective_from,
        # upper() returns exclusive upper for '[)' ranges — that's the schema default.
        # If upper is 'infinity' the lib will hand back None.
        effective_until=row.effective_until,
        approval=row.approval or {},
        created_at=row.created_at,
    )


def _to_op_run(row: Any) -> OperationalRun:
    return OperationalRun(
        id=row.id,
        tenant_id=row.tenant_id,
        operation_kind=OperationKind(row.operation_kind),
        environment=OperationalEnvironment(row.environment),
        execution_subject=row.execution_subject,
        authorization_ref=row.authorization_ref or {},
        started_at=row.started_at,
        finished_at=row.finished_at,
        status=OperationalRunStatus(row.status),
        evidence=row.evidence,
        created_at=row.created_at,
    )
