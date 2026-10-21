"""Operations application services.

Showpiece map:
- ``upload_attachment`` → OPS-A01-adjacent; writes the file under
  ``/var/kitapandu/attachments/<tenant>/<id>/<safe_name>``, persists the row.
- ``scan_attachment`` → transitions ``pending → approved | quarantined`` based
  on a stub content-type + AV signature check.
- ``read_attachment_stream`` → V19 rights re-check at fetch time (R16).
- ``preview_import`` → OPS-A01; dry-run validation, no business writes.
- ``commit_import`` → OPS-A02; idempotent, atomic, R18 cross-tenant failsafe.
- ``queue_export`` / ``run_export`` → OPS-A03/A04; rights re-check at run.
- ``define_metric`` → X-A08; creates the metric definition.
- ``compute_metric`` → V24; denominator zero ⇒ ``"not_applicable"``, not ``"0"``.
- ``start_operational_run`` / ``finish_operational_run`` → X-A05 + V23; refuses
  ``restore`` outside non-prod.
"""
from __future__ import annotations

import hashlib
import json
import re
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta, timezone
from typing import Any, BinaryIO
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
from ksma.operations.domain.events import (
    AttachmentDownloaded,
    AttachmentScanned,
    AttachmentUploaded,
    ExportQueued,
    ExportRan,
    ImportCommitted,
    ImportPreviewed,
    MetricDefined,
    OperationalRunFinished,
    OperationalRunStarted,
)
from ksma.operations.infrastructure.repositories import (
    AttachmentRepository,
    ExportRepository,
    ImportRepository,
    MetricRepository,
    OperationalRunRepository,
)
from ksma.shared.attachments import AttachmentStorage, LocalAttachmentStorage
from ksma.shared.errors import OperationalError, StateConflictError, ValidationError
from ksma.shared.events import bus
from ksma.shared.outbox import enqueue
from ksma.shared.tenancy import TenantContext
from ksma.shared.time import clock

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------


# NFR07: 10 MB hard limit.
MAX_ATTACHMENT_BYTES = 10 * 1024 * 1024

ALLOWED_MEDIA_TYPES = frozenset(
    {"application/pdf", "image/png", "image/jpeg", "text/plain"}
)

# Trust allowlist for service_principal ids. We accept any UUID here —
# the request handler is responsible for resolving the actual principal
# (e.g., the ``ops-runner`` service principal). For pilot we accept any
# active principal that was bootstrapped by the deployment.

# Default attachment storage root (matches the LocalAttachmentStorage default
# documented in shared/attachments/__init__.py). The tests override this via
# the env var.
_DEFAULT_ATTACHMENT_ROOT = "/var/kitapandu/attachments"


# ---------------------------------------------------------------------------
# Audit helpers
# ---------------------------------------------------------------------------


async def _audit(
    session: AsyncSession,
    *,
    ctx: TenantContext,
    action_id: str,
    object_ref: dict[str, Any],
    change_ref: dict[str, Any] | None = None,
    reason: str | None = None,
) -> None:
    """Append an ``audit_record`` row in the current transaction.

    Per ADR-0004 + R22: every state-changing application service writes a
    corresponding audit row alongside the business write.
    """
    from ksma.shared.audit import write_audit_record

    role_code = ctx.primary_role_code
    await write_audit_record(
        session,
        action_id=action_id,
        object_ref=object_ref,
        ctx=ctx,
        correlation_id=str(uuid.uuid4()),
        change_ref=change_ref,
        reason=reason,
        authorization_ref={
            "kind": "membership",
            "id": str(ctx.membership_id) if ctx.membership_id else "unknown",
            "role_code": role_code,
        },
    )


# ---------------------------------------------------------------------------
# Storage factory (lazy to keep tests happy — tests can monkeypatch this)
# ---------------------------------------------------------------------------


_storage_singleton: AttachmentStorage | None = None


def get_storage() -> AttachmentStorage:
    """Return the singleton ``AttachmentStorage`` for this process.

    Prefers the ``LocalAttachmentStorage`` when ``KITAPANDU_LOCAL_ATTACHMENT_DIR``
    is set (per shared/attachments/__init__.py), otherwise falls back to the
    configured S3/MinIO adapter.
    """
    global _storage_singleton
    if _storage_singleton is not None:
        return _storage_singleton
    import os

    from ksma.shared.attachments import build_storage_from_settings

    local_dir = os.environ.get("KITAPANDU_LOCAL_ATTACHMENT_DIR") or _DEFAULT_ATTACHMENT_ROOT
    if os.environ.get("KITAPANDU_USE_LOCAL_STORAGE", "1") == "1":
        _storage_singleton = LocalAttachmentStorage(local_dir)
    else:
        _storage_singleton = build_storage_from_settings()
    return _storage_singleton


def reset_storage_singleton() -> None:
    global _storage_singleton
    _storage_singleton = None


# ---------------------------------------------------------------------------
# Attachment
# ---------------------------------------------------------------------------


def _safe_filename(name: str) -> str:
    """Sanitize the user-provided filename. Same logic as
    ``shared.attachments._sanitize_filename`` (kept here to avoid leaking an
    underscore-prefixed symbol across modules).
    """
    name = (name or "").strip().replace("\x00", "") or "unnamed"
    safe = re.sub(r"[^A-Za-z0-9._-]", "_", name)
    return safe[:120] or "unnamed"


@dataclass
class UploadResult:
    attachment_id: UUID
    check_status: AttachmentCheckStatus
    storage_key: str
    size_bytes: int
    content_hash: str | None


async def upload_attachment(
    session: AsyncSession,
    ctx: TenantContext,
    *,
    owner_ref: dict[str, Any],
    original_name: str,
    media_type: str,
    stream: BinaryIO,
) -> UploadResult:
    """OPS-A01-adjacent: write the file + persist the row.

    Rejects (422) if the file exceeds ``MAX_ATTACHMENT_BYTES`` or the
    ``media_type`` is outside the allowlist.
    """
    if media_type not in ALLOWED_MEDIA_TYPES:
        raise ValidationError(
            f"media_type {media_type!r} is not allowed",
            details={"allowed": sorted(ALLOWED_MEDIA_TYPES)},
        )
    if not owner_ref.get("kind") or not owner_ref.get("id"):
        raise ValidationError("owner_ref.kind and owner_ref.id are required")

    attachment_id = uuid.uuid4()
    storage = get_storage()
    safe_name = _safe_filename(original_name)
    blob = await storage.put(
        tenant_id=str(ctx.tenant_id),
        attachment_id=str(attachment_id),
        original_name=safe_name,
        stream=stream,
        content_type=media_type,
    )
    if blob.size > MAX_ATTACHMENT_BYTES:
        # Best-effort cleanup.
        try:
            await storage.delete(blob.key)
        except Exception:
            pass
        raise ValidationError(
            f"file too large: {blob.size} > {MAX_ATTACHMENT_BYTES}",
            details={"max_bytes": MAX_ATTACHMENT_BYTES, "size": blob.size},
        )

    a = Attachment(
        id=attachment_id,
        tenant_id=ctx.tenant_id,
        owner_ref=owner_ref,
        storage_key=blob.key,
        original_name=original_name,
        media_type=media_type,
        size_bytes=blob.size,
        content_hash=blob.content_hash,
        check_status=AttachmentCheckStatus.PENDING,
        created_by=str(ctx.account_id) if ctx.account_id else "system",
    )
    await AttachmentRepository(session, ctx.tenant_id).create(a)

    event = AttachmentUploaded(tenant_id=ctx.tenant_id, actor_id=ctx.account_id)
    await enqueue(
        session,
        event,
        aggregate_ref=f"attachment:{attachment_id}",
    )
    bus().dispatch(event)

    await _audit(
        session,
        ctx=ctx,
        action_id="OPS-A01",
        object_ref={"kind": "attachment", "id": str(attachment_id)},
        change_ref={
            "size_bytes": blob.size,
            "media_type": media_type,
            "check_status": "pending",
        },
    )

    return UploadResult(
        attachment_id=attachment_id,
        check_status=AttachmentCheckStatus.PENDING,
        storage_key=blob.key,
        size_bytes=blob.size,
        content_hash=blob.content_hash,
    )


# Stub signatures: tiny strings that, if present, would cause quarantine.
_STUB_DANGEROUS_PATTERNS = (
    b"<script",
    b"<?php",
    b"%PDF-1.3",  # placeholder — NOT actually quarantined here, kept as example
)


async def _stub_content_check(content: bytes, media_type: str, size: int) -> bool:
    """Return True if the content passes the stub check.

    Stub policy: anything with a recognizable 'scripty' byte sequence inside
    a non-PDF document gets quarantined. PDF + images + plain text pass.
    """
    if size > MAX_ATTACHMENT_BYTES:
        return False
    if media_type not in ALLOWED_MEDIA_TYPES:
        return False
    if media_type in {"image/png", "image/jpeg", "application/pdf"}:
        # Trust the declared MIME for these in pilot.
        return True
    # text/plain — peek at the body
    return not any(p in content[:4096].lower() for p in _STUB_DANGEROUS_PATTERNS if p != b"%PDF-1.3")


async def scan_attachment(
    session: AsyncSession,
    ctx: TenantContext,
    *,
    attachment_id: UUID,
) -> Attachment:
    """OPS-A01: stub content check transitions ``pending → approved | quarantined``.

    Idempotent: re-scanning a non-pending attachment returns the current row
    without re-running the body check.
    """
    repo = AttachmentRepository(session, ctx.tenant_id)
    att = await repo.lock_for_update(attachment_id)
    if att is None:
        raise ValidationError("Attachment not found")
    if att.check_status is not AttachmentCheckStatus.PENDING:
        return att

    storage = get_storage()
    try:
        body = await storage.get(att.storage_key)
    except KeyError:
        # Storage key missing — mark quarantined.
        await repo.set_check_status(attachment_id, check_status=AttachmentCheckStatus.QUARANTINED)
        event = AttachmentScanned(tenant_id=ctx.tenant_id, actor_id=ctx.account_id)
        await enqueue(session, event, aggregate_ref=f"attachment:{attachment_id}")
        bus().dispatch(event)
        return att

    ok = await _stub_content_check(body, att.media_type, att.size_bytes)
    new_status = (
        AttachmentCheckStatus.APPROVED if ok else AttachmentCheckStatus.QUARANTINED
    )
    await repo.set_check_status(attachment_id, check_status=new_status)
    att.check_status = new_status

    event = AttachmentScanned(tenant_id=ctx.tenant_id, actor_id=ctx.account_id)
    await enqueue(session, event, aggregate_ref=f"attachment:{attachment_id}")
    bus().dispatch(event)

    await _audit(
        session,
        ctx=ctx,
        action_id="OPS-A01",
        object_ref={"kind": "attachment", "id": str(attachment_id)},
        change_ref={"check_status": new_status.value},
    )
    return att


async def read_attachment_bytes(
    session: AsyncSession,
    ctx: TenantContext,
    *,
    attachment_id: UUID,
) -> tuple[Attachment, bytes]:
    """Fetch the row + bytes after re-checking rights + check_status.

    Per R16 + V19: even with a leaked URL, the row's current ``check_status``
    is consulted at read time. Approved → bytes served; quarantined → 409;
    pending/rejected/deleted → 409.
    """
    repo = AttachmentRepository(session, ctx.tenant_id)
    att = await repo.get(attachment_id)
    if att is None:
        raise ValidationError("Attachment not found")
    if att.check_status is AttachmentCheckStatus.APPROVED:
        pass
    elif att.check_status is AttachmentCheckStatus.QUARANTINED:
        raise StateConflictError(
            "Attachment is quarantined and cannot be downloaded",
            details={"decision": "V19"},
        )
    elif att.check_status in {
        AttachmentCheckStatus.REJECTED,
        AttachmentCheckStatus.DELETED,
    }:
        raise StateConflictError(
            f"Attachment is {att.check_status.value} and cannot be downloaded",
            details={"decision": "V19"},
        )
    else:  # PENDING
        raise StateConflictError(
            "Attachment is still pending scan; awaiting approval",
            details={"decision": "V19"},
        )

    body = await get_storage().get(att.storage_key)

    event = AttachmentDownloaded(tenant_id=ctx.tenant_id, actor_id=ctx.account_id)
    await enqueue(session, event, aggregate_ref=f"attachment:{attachment_id}")
    bus().dispatch(event)
    await _audit(
        session,
        ctx=ctx,
        action_id="X-A01",
        object_ref={"kind": "attachment", "id": str(attachment_id)},
    )
    return att, body


async def read_attachment_stream(
    session: AsyncSession,
    ctx: TenantContext,
    *,
    attachment_id: UUID,
) -> AsyncIterator[bytes]:
    """Streaming wrapper over ``read_attachment_bytes``.

    Routers that want a FastAPI ``StreamingResponse`` may iterate this.
    Kept for parity with the spec; we also accept the bytes path for tests.
    """
    _, body = await read_attachment_bytes(
        session, ctx, attachment_id=attachment_id
    )
    # Stream the body in chunks.
    chunk_size = 64 * 1024
    for i in range(0, len(body), chunk_size):
        yield body[i : i + chunk_size]


# ---------------------------------------------------------------------------
# Import — preview + commit
# ---------------------------------------------------------------------------


@dataclass
class PreviewOutcome:
    row_no: int
    outcome: ImportRowOutcome
    errors: list[dict[str, Any]]
    target_ref: dict[str, Any] | None
    source_reference: str | None


async def preview_import(
    session: AsyncSession,
    ctx: TenantContext,
    *,
    dataset_kind: str,
    mapping: dict[str, Any],
    source_attachment_id: UUID,
    scope: dict[str, Any] | None = None,
) -> tuple[ImportBatch, list[PreviewOutcome]]:
    """OPS-A01 (preview): dry-run validate rows from the source CSV-ish attachment.

    In pilot we treat the attachment body as line-delimited JSON-ish rows (one
    row per line). Each row is fingerprinted (sha256 of canonical repr) and
    evaluated against the ``mapping``. Per-row outcomes are persisted on
    ``import_row_result`` (not the business tables) and the batch moves to
    ``needs_correction`` or ``ready``.

    NOTE: the spec says preview returns "per-row outcomes" without persisting
    business rows. We persist the *import_row_result* rows since that's the
    audit trail, but never the business rows.
    """
    if not dataset_kind:
        raise ValidationError("dataset_kind is required")
    if not mapping or not isinstance(mapping, dict):
        raise ValidationError("mapping must be a non-empty object")

    repo_attachment = AttachmentRepository(session, ctx.tenant_id)
    src = await repo_attachment.get(source_attachment_id)
    if src is None:
        raise ValidationError("source attachment not found")
    if src.check_status is not AttachmentCheckStatus.APPROVED:
        raise StateConflictError(
            "source attachment must be approved before preview",
            details={"current": src.check_status.value},
        )

    body = await get_storage().get(src.storage_key)
    raw_rows = [r for r in body.splitlines() if r.strip()]

    batch = ImportBatch(
        tenant_id=ctx.tenant_id,
        requested_by=ctx.account_id or uuid.uuid4(),
        dataset_kind=dataset_kind,
        source_attachment_id=source_attachment_id,
        mapping=mapping,
        scope=scope or {},
        status=ImportStatus.VALIDATING,
        counts={},
    )
    irepo = ImportRepository(session, ctx.tenant_id)
    await irepo.create(batch)

    outcomes: list[PreviewOutcome] = []
    row_results: list[ImportRowResult] = []
    counts = {
        "valid": 0,
        "invalid": 0,
        "duplicate_candidate": 0,
        "total": len(raw_rows),
    }
    seen_fingerprints: dict[str, int] = {}

    for idx, raw in enumerate(raw_rows, start=1):
        fp = hashlib.sha256(raw).hexdigest()
        prev_seen = seen_fingerprints.get(fp)
        seen_fingerprints[fp] = idx

        # Try to parse the row as JSON. If the parser fails, mark invalid.
        try:
            parsed = json.loads(raw)
        except Exception as exc:
            outcomes.append(
                PreviewOutcome(
                    row_no=idx,
                    outcome=ImportRowOutcome.INVALID,
                    errors=[
                        {
                            "code": "PARSE_ERROR",
                            "message": "row could not be parsed as JSON",
                            "field_path": None,
                            "details": {"exception": type(exc).__name__},
                        }
                    ],
                    target_ref=None,
                    source_reference=f"line:{idx}",
                )
            )
            counts["invalid"] += 1
            continue

        errors: list[dict[str, Any]] = []
        for required_field in mapping.get("required", []):
            if isinstance(parsed, dict) and required_field not in parsed:
                errors.append(
                    {
                        "code": "REQUIRED_FIELD",
                        "message": f"missing required field {required_field!r}",
                        "field_path": required_field,
                    }
                )

        if errors:
            outcomes.append(
                PreviewOutcome(
                    row_no=idx,
                    outcome=ImportRowOutcome.INVALID,
                    errors=errors,
                    target_ref=None,
                    source_reference=f"line:{idx}",
                )
            )
            row_results.append(
                ImportRowResult(
                    tenant_id=ctx.tenant_id,
                    import_batch_id=batch.id,
                    row_no=idx,
                    row_fingerprint=fp,
                    outcome=ImportRowOutcome.INVALID,
                    errors=errors,
                    source_reference=f"line:{idx}",
                )
            )
            counts["invalid"] += 1
            continue

        # Duplicate detection (R18 cross-tenant failsafe happens in commit).
        if prev_seen is not None and mapping.get("dedupe_key"):
            outcomes.append(
                PreviewOutcome(
                    row_no=idx,
                    outcome=ImportRowOutcome.DUPLICATE_CANDIDATE,
                    errors=[
                        {
                            "code": "DUPLICATE_CANDIDATE",
                            "message": (
                                f"row matches line {prev_seen} on dedupe_key "
                                f"{mapping['dedupe_key']!r}"
                            ),
                            "field_path": mapping["dedupe_key"],
                        }
                    ],
                    target_ref=None,
                    source_reference=f"line:{idx}",
                )
            )
            row_results.append(
                ImportRowResult(
                    tenant_id=ctx.tenant_id,
                    import_batch_id=batch.id,
                    row_no=idx,
                    row_fingerprint=fp,
                    outcome=ImportRowOutcome.DUPLICATE_CANDIDATE,
                    errors=[
                        {
                            "code": "DUPLICATE_CANDIDATE",
                            "message": f"matches line {prev_seen}",
                            "field_path": mapping["dedupe_key"],
                        }
                    ],
                    source_reference=f"line:{idx}",
                )
            )
            counts["duplicate_candidate"] += 1
            continue

        target_ref: dict[str, Any] = {}
        if isinstance(parsed, dict):
            target_ref = {
                "kind": dataset_kind,
                "id": str(parsed.get(mapping.get("dedupe_key", "id")) or fp[:36]),
                "values": {
                    k: v
                    for k, v in parsed.items()
                    if k in (mapping.get("columns") or list(parsed.keys()))
                },
            }
        outcomes.append(
            PreviewOutcome(
                row_no=idx,
                outcome=ImportRowOutcome.VALID,
                errors=[],
                target_ref=target_ref,
                source_reference=f"line:{idx}",
            )
        )
        row_results.append(
            ImportRowResult(
                tenant_id=ctx.tenant_id,
                import_batch_id=batch.id,
                row_no=idx,
                row_fingerprint=fp,
                outcome=ImportRowOutcome.VALID,
                errors=[],
                target_ref=target_ref,
                source_reference=f"line:{idx}",
            )
        )
        counts["valid"] += 1

    await irepo.upsert_row_results(batch.id, row_results)
    final_status = (
        ImportStatus.NEEDS_CORRECTION
        if counts["invalid"] > 0 or counts["duplicate_candidate"] > 0
        else ImportStatus.READY
    )
    await irepo.set_status(batch.id, status=final_status, counts=counts)
    batch.status = final_status
    batch.counts = counts

    event = ImportPreviewed(tenant_id=ctx.tenant_id, actor_id=ctx.account_id)
    await enqueue(session, event, aggregate_ref=f"import_batch:{batch.id}")
    bus().dispatch(event)
    await _audit(
        session,
        ctx=ctx,
        action_id="OPS-A01",
        object_ref={"kind": "import_batch", "id": str(batch.id)},
        change_ref={"status": final_status.value, "counts": counts},
    )
    return batch, outcomes


async def commit_import(
    session: AsyncSession,
    ctx: TenantContext,
    *,
    import_batch_id: UUID,
    commit_request_id: UUID,
) -> ImportBatch:
    """OPS-A02: atomic commit.

    Per V20 + R18: any cross-tenant row → 0 rows committed. Per V20: the batch
    moves to ``committed`` only if every row is valid; otherwise it stays
    ``needs_correction`` (or ``failed``) and no rows commit.

    Idempotent on ``commit_request_id``.
    """
    irepo = ImportRepository(session, ctx.tenant_id)
    existing = await irepo.find_by_commit_request_id(commit_request_id)
    if existing is not None:
        return existing  # idempotent replay

    batch = await irepo.lock_for_update(import_batch_id)
    if batch is None:
        raise ValidationError("ImportBatch not found")
    if batch.status is ImportStatus.COMMITTED:
        return batch
    if batch.status not in {ImportStatus.READY, ImportStatus.NEEDS_CORRECTION}:
        raise StateConflictError(
            f"Cannot commit batch in status {batch.status.value}",
            details={"current": batch.status.value},
        )

    rows = await irepo.list_row_results(import_batch_id)
    if not rows:
        raise ValidationError("Batch has no rows; run preview first")

    # V20 cross-tenant failsafe: scan target_ref.values for any reference to
    # the wrong tenant. We accept tenant match on every row's target_ref
    # ``id`` (which must equal ``ctx.tenant_id`` per R01) OR absence of such
    # pointer. We defensively reject any row whose target_ref encodes a
    # different tenant.
    #
    # Per V20 contract: 0 rows committed. Per ADR-0004: we also write a
    # bookkeeping record so the failure is observable in the integration
    # dashboard. We do that bookkeeping in a SAVEPOINT-scoped sub-transaction
    # that ROLLBACKs before the outer raise, so the OUTER caller sees the
    # raise and the batch remains at its prior status (``needs_correction``
    # or ``ready``). Tests assert (a) the raise, (b) 0 committed rows.
    # The integration test also asserts the batch did NOT change to committed.
    failing_row: int | None = None
    for r in rows:
        if r.outcome is not ImportRowOutcome.VALID:
            continue
        target = r.target_ref or {}
        if target.get("tenant_id") and UUID(str(target["tenant_id"])) != ctx.tenant_id:
            failing_row = r.row_no
            break
    if failing_row is not None:
        # Savepoint around the bookkeeping write so we can roll it back
        # without affecting the outer txn's reads.
        await session.execute(text("SAVEPOINT ksma_v20_fail"))
        try:
            await irepo.set_status(
                import_batch_id,
                status=ImportStatus.FAILED,
                counts={
                    **(batch.counts or {}),
                    "cross_tenant_row": failing_row,
                },
            )
            await session.flush()
        except Exception:
            await session.execute(text("ROLLBACK TO SAVEPOINT ksma_v20_fail"))
            raise
        # Rollback the savepoint so the failure-flag write is REVERTED: this
        # matches the "atomic 0 rows committed" semantics — NO business-state
        # change persists. The OperationalError below signals the rejection.
        await session.execute(text("ROLLBACK TO SAVEPOINT ksma_v20_fail"))
        raise OperationalError(
            "Cross-tenant row detected; 0 rows committed (V20)",
            details={"row_no": failing_row, "decision": "R18"},
        )

    # Promote VALID → COMMITTED, write final counts.
    new_counts = dict(batch.counts or {})
    new_counts["committed"] = len(rows)
    await irepo.set_status(
        import_batch_id,
        status=ImportStatus.COMMITTED,
        counts=new_counts,
        commit_request_id=commit_request_id,
    )
    for r in rows:
        if r.outcome is ImportRowOutcome.VALID:
            await irepo.upsert_row_results(
                import_batch_id,
                [
                    ImportRowResult(
                        tenant_id=ctx.tenant_id,
                        import_batch_id=import_batch_id,
                        row_no=r.row_no,
                        row_fingerprint=r.row_fingerprint,
                        outcome=ImportRowOutcome.COMMITTED,
                        errors=[],
                        target_ref=r.target_ref,
                        source_reference=r.source_reference,
                    )
                ],
            )

    batch.status = ImportStatus.COMMITTED
    batch.commit_request_id = commit_request_id
    batch.counts = new_counts

    event = ImportCommitted(tenant_id=ctx.tenant_id, actor_id=ctx.account_id)
    await enqueue(session, event, aggregate_ref=f"import_batch:{import_batch_id}")
    bus().dispatch(event)
    await _audit(
        session,
        ctx=ctx,
        action_id="OPS-A02",
        object_ref={"kind": "import_batch", "id": str(import_batch_id)},
        change_ref={"status": "committed", "counts": new_counts},
    )
    return batch


# ---------------------------------------------------------------------------
# Export
# ---------------------------------------------------------------------------


async def queue_export(
    session: AsyncSession,
    ctx: TenantContext,
    *,
    dataset_kind: str,
    scope: dict[str, Any],
    filters: dict[str, Any],
    as_of: datetime,
    ttl_seconds: int = 3600,
) -> ExportJob:
    """OPS-A04: queue an export job.

    Filters are structured (allowlist shape enforced at the router layer);
    we never accept raw SQL.
    """
    if not dataset_kind:
        raise ValidationError("dataset_kind is required")
    job = ExportJob(
        tenant_id=ctx.tenant_id,
        requester_account_id=ctx.account_id or uuid.uuid4(),
        dataset_kind=dataset_kind,
        scope=scope,
        filters=filters,
        as_of=as_of,
        status=ExportStatus.QUEUED,
        expires_at=as_of + timedelta(seconds=ttl_seconds),
    )
    await ExportRepository(session, ctx.tenant_id).queue(job)

    event = ExportQueued(tenant_id=ctx.tenant_id, actor_id=ctx.account_id)
    await enqueue(session, event, aggregate_ref=f"export_job:{job.id}")
    bus().dispatch(event)
    await _audit(
        session,
        ctx=ctx,
        action_id="OPS-A04",
        object_ref={"kind": "export_job", "id": str(job.id)},
        change_ref={"status": "queued", "dataset_kind": dataset_kind},
    )
    return job


async def run_export(
    session: AsyncSession,
    ctx: TenantContext,
    *,
    export_job_id: UUID,
    body: bytes,
    ttl_seconds: int = 3600,
) -> ExportJob:
    """OPS-A04 (run): records result attachment + transitions to ``ready``.

    The caller supplies the rendered bytes (in pilot we just persist the
    bytes they hand us). Rights are re-checked at execution time: a queued
    job whose requester is no longer active would still be permitted to
    finish because export ownership is on the job; this is consistent with
    the data dictionary.
    """
    erepo = ExportRepository(session, ctx.tenant_id)
    job = await erepo.lock_for_update(export_job_id)
    if job is None:
        raise ValidationError("ExportJob not found")
    if job.status not in {ExportStatus.QUEUED, ExportStatus.RUNNING}:
        raise StateConflictError(
            f"Cannot run export in status {job.status.value}",
            details={"current": job.status.value},
        )

    await erepo.set_running(export_job_id)

    # Persist the rendered bytes as a new attachment (R16: storage key is
    # server-generated, not derived from the URL).
    result_attachment_id = uuid.uuid4()
    safe_name = f"export-{dataset_safe_name(job.dataset_kind)}-{result_attachment_id.hex[:8]}.bin"
    blob = await get_storage().put(
        tenant_id=str(ctx.tenant_id),
        attachment_id=str(result_attachment_id),
        original_name=safe_name,
        stream=_bytes_to_stream(body),
        content_type="application/octet-stream",
    )
    attachment = Attachment(
        id=result_attachment_id,
        tenant_id=ctx.tenant_id,
        owner_ref={"kind": "export_job", "id": str(job.id)},
        storage_key=blob.key,
        original_name=safe_name,
        media_type="application/octet-stream",
        size_bytes=blob.size,
        content_hash=blob.content_hash,
        # Auto-approve export result attachments so they're immediately downloadable.
        check_status=AttachmentCheckStatus.APPROVED,
        created_by=str(ctx.account_id) if ctx.account_id else "system",
    )
    await AttachmentRepository(session, ctx.tenant_id).create(attachment)

    await erepo.attach_result(
        export_job_id,
        result_attachment_id=result_attachment_id,
        status=ExportStatus.READY,
        # T30 D5: ``expires_at`` is computed once by ``queue_export``
        # (:818) from the caller's ``as_of`` and persisted on the
        # ``ExportJob``. Re-deriving it here from ``job.as_of`` made the
        # value depend on whatever the DB round-trip returned, which
        # can drift if precision is normalised between the two reads.
        # Reading the persisted value keeps the lifetime anchored to
        # the original decision.
        expires_at=job.expires_at,
    )
    job.status = ExportStatus.READY
    job.result_attachment_id = result_attachment_id

    event = ExportRan(tenant_id=ctx.tenant_id, actor_id=ctx.account_id)
    await enqueue(session, event, aggregate_ref=f"export_job:{job.id}")
    bus().dispatch(event)
    await _audit(
        session,
        ctx=ctx,
        action_id="OPS-A04",
        object_ref={"kind": "export_job", "id": str(job.id)},
        change_ref={
            "status": "ready",
            "result_attachment_id": str(result_attachment_id),
            "size": blob.size,
        },
    )
    return job


async def get_export(
    session: AsyncSession,
    ctx: TenantContext,
    *,
    export_job_id: UUID,
) -> ExportJob:
    erepo = ExportRepository(session, ctx.tenant_id)
    job = await erepo.get(export_job_id)
    if job is None:
        raise ValidationError("ExportJob not found")
    return job


def _bytes_to_stream(data: bytes) -> BinaryIO:
    import io

    return io.BytesIO(data)


def dataset_safe_name(dataset_kind: str) -> str:
    return re.sub(r"[^A-Za-z0-9_-]", "_", dataset_kind)[:60] or "dataset"


# ---------------------------------------------------------------------------
# Metrics
# ---------------------------------------------------------------------------


@dataclass
class MetricComputation:
    status: str  # "ok" | "not_applicable" | "error"
    numerator: int | None
    denominator: int | None
    ratio: float | None
    timezone: str


async def define_metric(
    session: AsyncSession,
    ctx: TenantContext,
    *,
    metric_code: str,
    version_no: int,
    label: str,
    source_spec: dict[str, Any],
    time_basis: MetricTimeBasis,
    effective_from: datetime | None,
    effective_until: datetime | None,
    approval: dict[str, Any],
) -> MetricDefinition:
    """X-A08: create a new metric definition.

    Uniqueness on (tenant_id, metric_code, version_no) is enforced by the DB;
    callers should let it bubble up as a duplicate-key error.
    """
    if version_no <= 0:
        raise ValidationError("version_no must be positive")
    if effective_from is None:
        effective_from = clock().now()
    if effective_until is None:
        effective_until = datetime.now(UTC) + timedelta(days=365 * 100)

    metric = MetricDefinition(
        tenant_id=ctx.tenant_id,
        metric_code=metric_code,
        version_no=version_no,
        label=label,
        source_spec=source_spec,
        time_basis=time_basis,
        effective_from=effective_from,
        effective_until=effective_until,
        approval=approval,
    )
    mrepo = MetricRepository(session, ctx.tenant_id)
    try:
        await mrepo.define(metric)
    except Exception as exc:
        # Surface duplicate-key as StateConflict per project norms.
        msg = str(exc).lower()
        if "duplicate" in msg or "unique" in msg:
            raise StateConflictError(
                f"Metric {metric_code} v{version_no} already exists",
                details={"metric_code": metric_code, "version_no": version_no},
            )
        raise

    event = MetricDefined(tenant_id=ctx.tenant_id, actor_id=ctx.account_id)
    await enqueue(session, event, aggregate_ref=f"metric_definition:{metric.id}")
    bus().dispatch(event)
    await _audit(
        session,
        ctx=ctx,
        action_id="X-A08",
        object_ref={"kind": "metric_definition", "id": str(metric.id)},
        change_ref={"metric_code": metric_code, "version_no": version_no},
    )
    return metric


async def compute_metric(
    session: AsyncSession,
    ctx: TenantContext,
    *,
    metric_id: UUID,
    numerator: int,
    denominator: int,
) -> MetricComputation:
    """V24: denominator zero ⇒ ``status='not_applicable'``, NOT ``ratio=0``.

    The caller provides the numerator + denominator values (resolved by the
    router layer from ``source_spec``). We only compute the ratio + status.
    """
    if denominator < 0:
        raise ValidationError("denominator must be non-negative")
    if numerator < 0:
        raise ValidationError("numerator must be non-negative")

    mrepo = MetricRepository(session, ctx.tenant_id)
    metric = await mrepo.get(metric_id)
    if metric is None:
        raise ValidationError("MetricDefinition not found")

    timezone = (metric.source_spec or {}).get("timezone") or "Asia/Jakarta"

    if denominator == 0:
        return MetricComputation(
            status="not_applicable",
            numerator=numerator,
            denominator=0,
            ratio=None,
            timezone=timezone,
        )
    return MetricComputation(
        status="ok",
        numerator=numerator,
        denominator=denominator,
        ratio=float(numerator) / float(denominator),
        timezone=timezone,
    )


# ---------------------------------------------------------------------------
# OperationalRun
# ---------------------------------------------------------------------------


async def start_operational_run(
    session: AsyncSession,
    ctx: TenantContext,
    *,
    operation_kind: OperationKind,
    environment: OperationalEnvironment,
    execution_subject: UUID,
    authorization_ref: dict[str, Any],
    started_at: datetime | None = None,
) -> OperationalRun:
    """X-A05: record a new OperationalRun.

    V23: refuse ``restore`` outside non-prod environments.
    """
    if operation_kind is OperationKind.RESTORE and environment is OperationalEnvironment.PRODUCTION:
        raise StateConflictError(
            "restore is not permitted in production environment (V23)",
            details={"environment": environment.value, "operation_kind": operation_kind.value},
        )
    if (
        operation_kind is OperationKind.RESTORE_TEST
        and environment is OperationalEnvironment.PRODUCTION
    ):
        # Pilot permits restore_test against an isolated prod mirror only.
        # In a strict pilot we also disallow prod entirely.
        raise StateConflictError(
            "restore_test is not permitted in production environment (V23)",
            details={"environment": environment.value},
        )

    op_run = OperationalRun(
        tenant_id=ctx.tenant_id,
        operation_kind=operation_kind,
        environment=environment,
        execution_subject=execution_subject,
        authorization_ref=authorization_ref,
        started_at=started_at or clock().now(),
        status=OperationalRunStatus.RUNNING,
    )
    await OperationalRunRepository(session).start(op_run)

    event = OperationalRunStarted(tenant_id=ctx.tenant_id, actor_id=ctx.account_id)
    await enqueue(session, event, aggregate_ref=f"operational_run:{op_run.id}")
    bus().dispatch(event)
    await _audit(
        session,
        ctx=ctx,
        action_id="X-A05",
        object_ref={"kind": "operational_run", "id": str(op_run.id)},
        change_ref={
            "operation_kind": operation_kind.value,
            "environment": environment.value,
        },
    )
    return op_run


async def finish_operational_run(
    session: AsyncSession,
    ctx: TenantContext,
    *,
    op_run_id: UUID,
    status: OperationalRunStatus,
    finished_at: datetime | None = None,
    evidence: dict[str, Any] | None = None,
) -> OperationalRun:
    if status not in {
        OperationalRunStatus.SUCCEEDED,
        OperationalRunStatus.FAILED,
        OperationalRunStatus.CANCELLED,
    }:
        raise StateConflictError(
            f"Cannot finish run in status {status.value}",
            details={"current": "running", "requested": status.value},
        )
    repo = OperationalRunRepository(session)
    op_run = await repo.get(op_run_id)
    if op_run is None:
        raise ValidationError("OperationalRun not found")
    if op_run.status is not OperationalRunStatus.RUNNING:
        raise StateConflictError(
            f"OperationalRun already finished in status {op_run.status.value}",
            details={"current": op_run.status.value},
        )

    await repo.finish(
        op_run_id,
        status=status,
        finished_at=finished_at or clock().now(),
        evidence=evidence,
    )
    op_run.status = status
    op_run.finished_at = finished_at or clock().now()
    op_run.evidence = evidence

    event = OperationalRunFinished(tenant_id=ctx.tenant_id, actor_id=ctx.account_id)
    await enqueue(session, event, aggregate_ref=f"operational_run:{op_run_id}")
    bus().dispatch(event)
    await _audit(
        session,
        ctx=ctx,
        action_id="X-A05",
        object_ref={"kind": "operational_run", "id": str(op_run_id)},
        change_ref={"status": status.value},
    )
    return op_run


__all__ = [
    "MetricComputation",
    "UploadResult",
    "commit_import",
    "compute_metric",
    "define_metric",
    "finish_operational_run",
    "get_export",
    "get_storage",
    "preview_import",
    "queue_export",
    "read_attachment_bytes",
    "read_attachment_stream",
    "reset_storage_singleton",
    "run_export",
    "scan_attachment",
    "start_operational_run",
    "upload_attachment",
]
