"""Operations FastAPI router.

Endpoints (per issue 09-operations-slice.md):

- POST   /attachments             (multipart upload)             → OPS-A01-adjacent
- POST   /attachments/{id}/scan                                → OPS-A01
- GET    /attachments/{id}/content                            → V19 rights re-check
- POST   /imports/preview                                     → OPS-A01
- POST   /imports/commit (Idempotency-Key required)            → OPS-A02 / V20
- POST   /exports                                            → OPS-A04
- POST   /exports/{id}/run                                    → OPS-A04
- GET    /exports/{id}                                        → OPS-A04
- POST   /indicators                                          → X-A08
- GET    /indicators/{id}                                     → V24
- POST   /ops/runs                                            → X-A05 / V23
- PATCH  /ops/runs/{id}                                       → X-A05
"""
from __future__ import annotations

import io
from datetime import datetime
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, File, Form, Header, HTTPException, UploadFile, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, Field

from ksma.operations.application import (
    ALLOWED_MEDIA_TYPES,
    MAX_ATTACHMENT_BYTES,
    MetricTimeBasis,
    OperationalEnvironment,
    OperationalRunStatus,
    OperationKind,
    commit_import,
    compute_metric,
    define_metric,
    finish_operational_run,
    get_export,
    preview_import,
    queue_export,
    read_attachment_bytes,
    run_export,
    scan_attachment,
    start_operational_run,
    upload_attachment,
)
from ksma.operations.domain import (
    AttachmentCheckStatus,
)
from ksma.shared.persistence import session_scope
from ksma.shared.tenancy import TenantContext

router = APIRouter(prefix="/api/v1", tags=["operations"])


def require_tenant_context() -> TenantContext:  # pragma: no cover
    """Placeholder rebound by ``ksma.main.create_app`` to ``tenant_context_dep``."""
    raise NotImplementedError


# --- Schemas ----------------------------------------------------------------


class AttachmentOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: UUID
    owner_ref: dict[str, Any]
    storage_key: str
    original_name: str
    media_type: str
    size_bytes: int
    content_hash: str | None
    check_status: str


class ScanOut(BaseModel):
    id: UUID
    check_status: str


class PreviewRow(BaseModel):
    row_no: int
    outcome: str
    errors: list[dict[str, Any]]
    target_ref: dict[str, Any] | None = None
    source_reference: str | None = None


class ImportPreviewBody(BaseModel):
    dataset_kind: str = Field(min_length=1, max_length=120)
    mapping: dict[str, Any] = Field(default_factory=dict)
    source_attachment_id: UUID
    scope: dict[str, Any] = Field(default_factory=dict)


class ImportPreviewOut(BaseModel):
    batch_id: UUID
    status: str
    counts: dict[str, Any]
    rows: list[PreviewRow]


class ImportCommitBody(BaseModel):
    import_batch_id: UUID


class ImportCommitOut(BaseModel):
    batch_id: UUID
    status: str
    counts: dict[str, Any]


class ExportQueueBody(BaseModel):
    dataset_kind: str = Field(min_length=1, max_length=120)
    scope: dict[str, Any] = Field(default_factory=dict)
    filters: dict[str, Any] = Field(default_factory=dict)
    as_of: datetime
    ttl_seconds: int = Field(default=3600, ge=60, le=86_400)


class ExportRunBody(BaseModel):
    body_b64: str | None = Field(
        default=None,
        description="Optional base64 payload to attach (pilot only). Production paths would stream from a real source.",
    )


class ExportOut(BaseModel):
    id: UUID
    dataset_kind: str
    status: str
    result_attachment_id: UUID | None = None
    expires_at: datetime | None = None
    as_of: datetime


class MetricDefineBody(BaseModel):
    metric_code: str = Field(min_length=1, max_length=120)
    version_no: int = Field(ge=1)
    label: str = Field(min_length=1, max_length=240)
    source_spec: dict[str, Any] = Field(default_factory=dict)
    time_basis: MetricTimeBasis = MetricTimeBasis.RECORD_DATE
    effective_from: datetime | None = None
    effective_until: datetime | None = None
    approval: dict[str, Any] = Field(default_factory=dict)


class MetricOut(BaseModel):
    id: UUID
    metric_code: str
    version_no: int
    label: str
    time_basis: str
    source_spec: dict[str, Any]
    effective_from: datetime | None
    effective_until: datetime | None
    approval: dict[str, Any]


class MetricComputeBody(BaseModel):
    numerator: int = Field(ge=0)
    denominator: int = Field(ge=0)


class MetricComputeOut(BaseModel):
    metric_id: UUID
    status: str
    numerator: int | None
    denominator: int | None
    ratio: float | None
    timezone: str


class OpsRunStartBody(BaseModel):
    operation_kind: OperationKind
    environment: OperationalEnvironment
    execution_subject: UUID
    authorization_ref: dict[str, Any] = Field(default_factory=dict)
    started_at: datetime | None = None


class OpsRunOut(BaseModel):
    id: UUID
    operation_kind: str
    environment: str
    status: str
    started_at: datetime | None
    finished_at: datetime | None


class OpsRunFinishBody(BaseModel):
    status: OperationalRunStatus
    finished_at: datetime | None = None
    evidence: dict[str, Any] | None = None


# --- Attachment endpoints ---------------------------------------------------


@router.post(
    "/attachments",
    response_model=AttachmentOut,
    status_code=status.HTTP_201_CREATED,
)
async def post_attachment(
    owner_ref: str = Form(..., description='JSON-encoded ObjectRef e.g. {"kind":"participant","id":"..."}'),
    file: UploadFile = File(...),
    ctx: TenantContext = Depends(require_tenant_context),
) -> AttachmentOut:
    """OPS-A01-adjacent: multipart upload of a single file attachment.

    Enforces the NFR07 size limit AND the media-type allowlist.
    """
    import json as _json

    from ksma.shared.errors import ValidationError as _VE

    try:
        owner_ref_obj = _json.loads(owner_ref)
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={"code": "INVALID_VALUE", "message": f"owner_ref is not valid JSON: {exc}"},
        )
    if file.content_type not in ALLOWED_MEDIA_TYPES:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={
                "code": "INVALID_VALUE",
                "message": f"media_type {file.content_type!r} is not allowed",
                "details": {"allowed": sorted(ALLOWED_MEDIA_TYPES)},
            },
        )

    # Defense-in-depth: read no more than the cap.
    cap = MAX_ATTACHMENT_BYTES + 1
    body = await file.read(cap)
    if len(body) > MAX_ATTACHMENT_BYTES:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail={
                "code": "INVALID_VALUE",
                "message": f"file exceeds {MAX_ATTACHMENT_BYTES} bytes (NFR07)",
            },
        )

    async with session_scope() as session:
        try:
            result = await upload_attachment(
                session,
                ctx,
                owner_ref=owner_ref_obj,
                original_name=file.filename or "upload",
                media_type=file.content_type or "application/octet-stream",
                stream=io.BytesIO(body),
            )
        except _VE as ve:
            raise HTTPException(status_code=ve.http_status, detail=ve.to_payload())
        return AttachmentOut(
            id=result.attachment_id,
            owner_ref=owner_ref_obj,
            storage_key=result.storage_key,
            original_name=file.filename or "upload",
            media_type=file.content_type or "application/octet-stream",
            size_bytes=result.size_bytes,
            content_hash=result.content_hash,
            check_status=result.check_status.value,
        )


@router.post(
    "/attachments/{attachment_id}/scan",
    response_model=ScanOut,
)
async def post_scan_attachment(
    attachment_id: UUID,
    ctx: TenantContext = Depends(require_tenant_context),
) -> ScanOut:
    async with session_scope() as session:
        att = await scan_attachment(session, ctx, attachment_id=attachment_id)
        return ScanOut(id=att.id, check_status=att.check_status.value)


@router.get("/attachments/{attachment_id}/content")
async def get_attachment_content(
    attachment_id: UUID,
    ctx: TenantContext = Depends(require_tenant_context),
) -> StreamingResponse:
    async with session_scope() as session:
        try:
            att, body = await read_attachment_bytes(
                session, ctx, attachment_id=attachment_id
            )
        except Exception as e:
            from ksma.shared.errors import DomainError

            if isinstance(e, DomainError):
                raise HTTPException(status_code=e.http_status, detail=e.to_payload())
            raise
        return StreamingResponse(
            iter([body]),
            media_type=att.media_type,
            headers={
                "Content-Disposition": f'attachment; filename="{att.original_name}"',
                "X-KSMA-Check-Status": att.check_status.value,
            },
        )


# --- Import endpoints -------------------------------------------------------


@router.post("/imports/preview", response_model=ImportPreviewOut)
async def post_import_preview(
    body: ImportPreviewBody,
    ctx: TenantContext = Depends(require_tenant_context),
) -> ImportPreviewOut:
    async with session_scope() as session:
        try:
            batch, outcomes = await preview_import(
                session,
                ctx,
                dataset_kind=body.dataset_kind,
                mapping=body.mapping,
                source_attachment_id=body.source_attachment_id,
                scope=body.scope,
            )
        except Exception as e:
            from ksma.shared.errors import DomainError

            if isinstance(e, DomainError):
                raise HTTPException(status_code=e.http_status, detail=e.to_payload())
            raise
        return ImportPreviewOut(
            batch_id=batch.id,
            status=batch.status.value,
            counts=batch.counts,
            rows=[
                PreviewRow(
                    row_no=o.row_no,
                    outcome=o.outcome.value,
                    errors=o.errors,
                    target_ref=o.target_ref,
                    source_reference=o.source_reference,
                )
                for o in outcomes
            ],
        )


@router.post("/imports/commit", response_model=ImportCommitOut)
async def post_import_commit(
    body: ImportCommitBody,
    idempotency_key: str = Header(..., alias="Idempotency-Key", min_length=8, max_length=200),
    ctx: TenantContext = Depends(require_tenant_context),
) -> ImportCommitOut:
    """OPS-A02: atomic commit. Requires ``Idempotency-Key`` header."""
    import hashlib

    key_fp = hashlib.sha256(idempotency_key.encode()).hexdigest()
    commit_request_id = UUID(hex=key_fp[:32])

    async with session_scope() as session:
        try:
            result = await commit_import(
                session,
                ctx,
                import_batch_id=body.import_batch_id,
                commit_request_id=commit_request_id,
            )
        except Exception as e:
            from ksma.shared.errors import DomainError

            if isinstance(e, DomainError):
                raise HTTPException(status_code=e.http_status, detail=e.to_payload())
            raise
        return ImportCommitOut(
            batch_id=result.id,
            status=result.status.value,
            counts=result.counts,
        )


# --- Export endpoints -------------------------------------------------------


@router.post(
    "/exports",
    response_model=ExportOut,
    status_code=status.HTTP_201_CREATED,
)
async def post_export(
    body: ExportQueueBody,
    ctx: TenantContext = Depends(require_tenant_context),
) -> ExportOut:
    async with session_scope() as session:
        job = await queue_export(
            session,
            ctx,
            dataset_kind=body.dataset_kind,
            scope=body.scope,
            filters=body.filters,
            as_of=body.as_of,
            ttl_seconds=body.ttl_seconds,
        )
        return ExportOut(
            id=job.id,
            dataset_kind=job.dataset_kind,
            status=job.status.value,
            result_attachment_id=job.result_attachment_id,
            expires_at=job.expires_at,
            as_of=job.as_of,
        )


@router.post("/exports/{export_job_id}/run", response_model=ExportOut)
async def post_export_run(
    export_job_id: UUID,
    body: ExportRunBody = ExportRunBody(),
    ctx: TenantContext = Depends(require_tenant_context),
) -> ExportOut:
    import base64

    if body.body_b64:
        raw = base64.b64decode(body.body_b64)
    else:
        # Placeholder body when the caller does not supply one.
        raw = b"<empty>"

    async with session_scope() as session:
        try:
            job = await run_export(
                session, ctx, export_job_id=export_job_id, body=raw
            )
        except Exception as e:
            from ksma.shared.errors import DomainError

            if isinstance(e, DomainError):
                raise HTTPException(status_code=e.http_status, detail=e.to_payload())
            raise
        return ExportOut(
            id=job.id,
            dataset_kind=job.dataset_kind,
            status=job.status.value,
            result_attachment_id=job.result_attachment_id,
            expires_at=job.expires_at,
            as_of=job.as_of,
        )


@router.get("/exports/{export_job_id}", response_model=ExportOut)
async def get_export_endpoint(
    export_job_id: UUID,
    ctx: TenantContext = Depends(require_tenant_context),
) -> ExportOut:
    async with session_scope() as session:
        job = await get_export(session, ctx, export_job_id=export_job_id)
        return ExportOut(
            id=job.id,
            dataset_kind=job.dataset_kind,
            status=job.status.value,
            result_attachment_id=job.result_attachment_id,
            expires_at=job.expires_at,
            as_of=job.as_of,
        )


# --- Indicator endpoints ----------------------------------------------------


@router.post(
    "/indicators",
    response_model=MetricOut,
    status_code=status.HTTP_201_CREATED,
)
async def post_indicator(
    body: MetricDefineBody,
    ctx: TenantContext = Depends(require_tenant_context),
) -> MetricOut:
    async with session_scope() as session:
        try:
            metric = await define_metric(
                session,
                ctx,
                metric_code=body.metric_code,
                version_no=body.version_no,
                label=body.label,
                source_spec=body.source_spec,
                time_basis=body.time_basis,
                effective_from=body.effective_from,
                effective_until=body.effective_until,
                approval=body.approval,
            )
        except Exception as e:
            from ksma.shared.errors import DomainError

            if isinstance(e, DomainError):
                raise HTTPException(status_code=e.http_status, detail=e.to_payload())
            raise
        return MetricOut(
            id=metric.id,
            metric_code=metric.metric_code,
            version_no=metric.version_no,
            label=metric.label,
            time_basis=metric.time_basis.value,
            source_spec=metric.source_spec,
            effective_from=metric.effective_from,
            effective_until=metric.effective_until,
            approval=metric.approval,
        )


@router.post("/indicators/{metric_id}/compute", response_model=MetricComputeOut)
async def post_compute_indicator(
    metric_id: UUID,
    body: MetricComputeBody,
    ctx: TenantContext = Depends(require_tenant_context),
) -> MetricComputeOut:
    async with session_scope() as session:
        try:
            comp = await compute_metric(
                session,
                ctx,
                metric_id=metric_id,
                numerator=body.numerator,
                denominator=body.denominator,
            )
        except Exception as e:
            from ksma.shared.errors import DomainError

            if isinstance(e, DomainError):
                raise HTTPException(status_code=e.http_status, detail=e.to_payload())
            raise
        return MetricComputeOut(
            metric_id=metric_id,
            status=comp.status,
            numerator=comp.numerator,
            denominator=comp.denominator,
            ratio=comp.ratio,
            timezone=comp.timezone,
        )


@router.get("/indicators/{metric_id}", response_model=MetricOut)
async def get_indicator(
    metric_id: UUID,
    ctx: TenantContext = Depends(require_tenant_context),
) -> MetricOut:
    from ksma.operations.infrastructure.repositories import MetricRepository

    async with session_scope() as session:
        m = await MetricRepository(session, ctx.tenant_id).get(metric_id)
        if m is None:
            raise HTTPException(status_code=404, detail={"code": "NOT_FOUND"})
        return MetricOut(
            id=m.id,
            metric_code=m.metric_code,
            version_no=m.version_no,
            label=m.label,
            time_basis=m.time_basis.value,
            source_spec=m.source_spec,
            effective_from=m.effective_from,
            effective_until=m.effective_until,
            approval=m.approval,
        )


# --- OperationalRun endpoints ----------------------------------------------


@router.post(
    "/ops/runs",
    response_model=OpsRunOut,
    status_code=status.HTTP_201_CREATED,
)
async def post_ops_run(
    body: OpsRunStartBody,
    ctx: TenantContext = Depends(require_tenant_context),
) -> OpsRunOut:
    async with session_scope() as session:
        try:
            op_run = await start_operational_run(
                session,
                ctx,
                operation_kind=body.operation_kind,
                environment=body.environment,
                execution_subject=body.execution_subject,
                authorization_ref=body.authorization_ref,
                started_at=body.started_at,
            )
        except Exception as e:
            from ksma.shared.errors import DomainError

            if isinstance(e, DomainError):
                raise HTTPException(status_code=e.http_status, detail=e.to_payload())
            raise
        return OpsRunOut(
            id=op_run.id,
            operation_kind=op_run.operation_kind.value,
            environment=op_run.environment.value,
            status=op_run.status.value,
            started_at=op_run.started_at,
            finished_at=op_run.finished_at,
        )


@router.patch("/ops/runs/{op_run_id}", response_model=OpsRunOut)
async def patch_ops_run(
    op_run_id: UUID,
    body: OpsRunFinishBody,
    ctx: TenantContext = Depends(require_tenant_context),
) -> OpsRunOut:
    async with session_scope() as session:
        try:
            op_run = await finish_operational_run(
                session,
                ctx,
                op_run_id=op_run_id,
                status=body.status,
                finished_at=body.finished_at,
                evidence=body.evidence,
            )
        except Exception as e:
            from ksma.shared.errors import DomainError

            if isinstance(e, DomainError):
                raise HTTPException(status_code=e.http_status, detail=e.to_payload())
            raise
        return OpsRunOut(
            id=op_run.id,
            operation_kind=op_run.operation_kind.value,
            environment=op_run.environment.value,
            status=op_run.status.value,
            started_at=op_run.started_at,
            finished_at=op_run.finished_at,
        )
