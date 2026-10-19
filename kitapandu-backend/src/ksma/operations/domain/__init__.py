"""Operations domain entities + value objects + enums.

Mirrors the schema created in migration ``2026_10_20_0240_0009_portal_operations``.
Every dataclass exposes only the columns the application layer cares about;
raw-row → entity mapping lives in the repository layer.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any
from uuid import UUID, uuid4

# ---------------------------------------------------------------------------
# Enums (mirror the CHECK constraints on the tables)
# ---------------------------------------------------------------------------


class AttachmentCheckStatus(StrEnum):
    PENDING = "pending"
    QUARANTINED = "quarantined"
    APPROVED = "approved"
    REJECTED = "rejected"
    DELETED = "deleted"


class ImportStatus(StrEnum):
    UPLOADED = "uploaded"
    VALIDATING = "validating"
    NEEDS_CORRECTION = "needs_correction"
    READY = "ready"
    COMMITTED = "committed"
    FAILED = "failed"


class ImportRowOutcome(StrEnum):
    VALID = "valid"
    INVALID = "invalid"
    DUPLICATE_CANDIDATE = "duplicate_candidate"
    COMMITTED = "committed"


class ExportStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    READY = "ready"
    FAILED = "failed"
    CANCELLED = "cancelled"
    EXPIRED = "expired"


class MetricTimeBasis(StrEnum):
    SERVICE_DATE = "service_date"
    RECORD_DATE = "record_date"
    DUE_DATE = "due_date"


class OperationalEnvironment(StrEnum):
    DEVELOPMENT = "development"
    TEST = "test"
    PRODUCTION = "production"


class OperationKind(StrEnum):
    BACKUP = "backup"
    RESTORE_TEST = "restore_test"
    RESTORE = "restore"
    RETENTION = "retention"
    RECONCILIATION = "reconciliation"


class OperationalRunStatus(StrEnum):
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


class BackgroundJobStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    RETRY_WAIT = "retry_wait"
    FAILED = "failed"
    CANCELLED = "cancelled"


# ---------------------------------------------------------------------------
# Entities
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class Attachment:
    id: UUID = field(default_factory=uuid4)
    tenant_id: UUID = field(default_factory=uuid4)
    owner_ref: dict[str, Any] = field(default_factory=dict)
    storage_key: str = ""
    original_name: str = ""
    media_type: str = ""
    size_bytes: int = 0
    content_hash: str | None = None
    check_status: AttachmentCheckStatus = AttachmentCheckStatus.PENDING
    retention_policy_id: UUID | None = None
    created_at: datetime | None = None
    created_by: str = ""


@dataclass(slots=True)
class ImportBatch:
    id: UUID = field(default_factory=uuid4)
    tenant_id: UUID = field(default_factory=uuid4)
    requested_by: UUID = field(default_factory=uuid4)
    dataset_kind: str = ""
    source_attachment_id: UUID | None = None
    mapping: dict[str, Any] = field(default_factory=dict)
    scope: dict[str, Any] = field(default_factory=dict)
    status: ImportStatus = ImportStatus.UPLOADED
    commit_request_id: UUID | None = None
    counts: dict[str, Any] = field(default_factory=dict)
    created_at: datetime | None = None
    updated_at: datetime | None = None


@dataclass(slots=True)
class ImportRowResult:
    id: UUID = field(default_factory=uuid4)
    tenant_id: UUID = field(default_factory=uuid4)
    import_batch_id: UUID = field(default_factory=uuid4)
    row_no: int = 0
    row_fingerprint: str = ""
    outcome: ImportRowOutcome = ImportRowOutcome.VALID
    errors: list[dict[str, Any]] | None = None
    target_ref: dict[str, Any] | None = None
    source_reference: str | None = None


@dataclass(slots=True)
class ExportJob:
    id: UUID = field(default_factory=uuid4)
    tenant_id: UUID = field(default_factory=uuid4)
    requester_account_id: UUID = field(default_factory=uuid4)
    dataset_kind: str = ""
    scope: dict[str, Any] = field(default_factory=dict)
    filters: dict[str, Any] = field(default_factory=dict)
    as_of: datetime | None = None
    status: ExportStatus = ExportStatus.QUEUED
    result_attachment_id: UUID | None = None
    expires_at: datetime | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None


@dataclass(slots=True)
class MetricDefinition:
    id: UUID = field(default_factory=uuid4)
    tenant_id: UUID = field(default_factory=uuid4)
    metric_code: str = ""
    version_no: int = 1
    label: str = ""
    source_spec: dict[str, Any] = field(default_factory=dict)
    time_basis: MetricTimeBasis = MetricTimeBasis.RECORD_DATE
    # effective_range is exposed as ``(lower, upper)`` inclusive lower, exclusive upper.
    effective_from: datetime | None = None
    effective_until: datetime | None = None
    approval: dict[str, Any] = field(default_factory=dict)
    created_at: datetime | None = None


@dataclass(slots=True)
class OperationalRun:
    id: UUID = field(default_factory=uuid4)
    tenant_id: UUID | None = None  # nullable per schema
    operation_kind: OperationKind = OperationKind.BACKUP
    environment: OperationalEnvironment = OperationalEnvironment.DEVELOPMENT
    execution_subject: UUID = field(default_factory=uuid4)  # service_principal.id
    authorization_ref: dict[str, Any] = field(default_factory=dict)
    started_at: datetime | None = None
    finished_at: datetime | None = None
    status: OperationalRunStatus = OperationalRunStatus.RUNNING
    evidence: dict[str, Any] | None = None
    created_at: datetime | None = None


# ---------------------------------------------------------------------------
# Value objects for indicator sources (typed MetricSource per the spec)
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class MetricSource:
    """Typed indicator input. ``denominator == 0`` is meaningful — see V24."""

    numerator_kind: str = "count"  # count | sum
    numerator_ref: dict[str, Any] = field(default_factory=dict)
    denominator_kind: str = "count"  # count | sum
    denominator_ref: dict[str, Any] = field(default_factory=dict)
    timezone: str = "Asia/Jakarta"


__all__ = [
    # Enums
    "AttachmentCheckStatus",
    "ImportStatus",
    "ImportRowOutcome",
    "ExportStatus",
    "MetricTimeBasis",
    "OperationalEnvironment",
    "OperationKind",
    "OperationalRunStatus",
    "BackgroundJobStatus",
    # Entities
    "Attachment",
    "ImportBatch",
    "ImportRowResult",
    "ExportJob",
    "MetricDefinition",
    "OperationalRun",
    # Value objects
    "MetricSource",
]
