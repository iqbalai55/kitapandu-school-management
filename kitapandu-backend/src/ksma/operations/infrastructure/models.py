"""SQLAlchemy 2.x ORM models for the operations + portal contexts.

Mirrors migration ``2026_10_20_0240_0009_portal_operations`` and
``2026_10_20_0250_0010_schema_baseline``.

Notes:
- The CHECK constraints live in the migration. We mirror the column types +
  nullability here; the ORM is the read shape, not the source of truth.
- All T-scoped tables have RLS via migration 0009 + 0010. Application code
  must continue to call ``set_current_tenant(session, tid)`` before touching
  any of these.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB, TSTZRANGE
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


# ---------------------------------------------------------------------------
# Portal (carried over because the same migration creates them)
# ---------------------------------------------------------------------------


class ServiceRequestRow(Base):
    __tablename__ = "service_request"
    id: Mapped[UUID] = mapped_column(PgUUID(as_uuid=True), primary_key=True)
    tenant_id: Mapped[UUID] = mapped_column(PgUUID(as_uuid=True), ForeignKey("tenant.id"), nullable=False)
    requester_account_id: Mapped[UUID] = mapped_column(PgUUID(as_uuid=True), ForeignKey("account.id"), nullable=False)
    participant_id: Mapped[UUID] = mapped_column(PgUUID(as_uuid=True), ForeignKey("participant.id"), nullable=False)
    category: Mapped[str] = mapped_column(String(32), nullable=False)
    source_ref: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    assigned_membership_id: Mapped[UUID | None] = mapped_column(PgUUID(as_uuid=True), ForeignKey("membership.id"), nullable=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    resolution: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_by: Mapped[str] = mapped_column(Text, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_by: Mapped[str] = mapped_column(Text, nullable=False)


class InternalNotificationRow(Base):
    __tablename__ = "internal_notification"
    id: Mapped[UUID] = mapped_column(PgUUID(as_uuid=True), primary_key=True)
    tenant_id: Mapped[UUID] = mapped_column(PgUUID(as_uuid=True), ForeignKey("tenant.id"), nullable=False)
    recipient_account_id: Mapped[UUID] = mapped_column(PgUUID(as_uuid=True), ForeignKey("account.id"), nullable=False)
    source_ref: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    event_id: Mapped[UUID | None] = mapped_column(PgUUID(as_uuid=True), nullable=True)
    notification_kind: Mapped[str] = mapped_column(Text, nullable=False)
    summary: Mapped[str] = mapped_column(Text, nullable=False)
    delivery_state: Mapped[str] = mapped_column(String(32), nullable=False)
    read_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class CommunicationPreferenceRow(Base):
    __tablename__ = "communication_preference"
    id: Mapped[UUID] = mapped_column(PgUUID(as_uuid=True), primary_key=True)
    tenant_id: Mapped[UUID] = mapped_column(PgUUID(as_uuid=True), ForeignKey("tenant.id"), nullable=False)
    account_id: Mapped[UUID] = mapped_column(PgUUID(as_uuid=True), ForeignKey("account.id"), nullable=False)
    participant_id: Mapped[UUID | None] = mapped_column(PgUUID(as_uuid=True), ForeignKey("participant.id"), nullable=True)
    category: Mapped[str] = mapped_column(Text, nullable=False)
    channel: Mapped[str] = mapped_column(String(32), nullable=False)
    enabled: Mapped[bool] = mapped_column(nullable=False, default=True)
    contact_reference: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


# ---------------------------------------------------------------------------
# Operations — Attachment (T scope)
# ---------------------------------------------------------------------------


class AttachmentRow(Base):
    __tablename__ = "attachment"
    id: Mapped[UUID] = mapped_column(PgUUID(as_uuid=True), primary_key=True)
    tenant_id: Mapped[UUID] = mapped_column(PgUUID(as_uuid=True), ForeignKey("tenant.id"), nullable=False)
    owner_ref: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    storage_key: Mapped[str] = mapped_column(Text, nullable=False)
    original_name: Mapped[str] = mapped_column(Text, nullable=False)
    media_type: Mapped[str] = mapped_column(String(120), nullable=False)
    size_bytes: Mapped[int] = mapped_column(Integer, nullable=False)
    content_hash: Mapped[str | None] = mapped_column(Text, nullable=True)
    check_status: Mapped[str] = mapped_column(String(32), nullable=False)
    retention_policy_id: Mapped[UUID | None] = mapped_column(PgUUID(as_uuid=True), ForeignKey("policy_version.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_by: Mapped[str] = mapped_column(Text, nullable=False)

    __table_args__ = (
        CheckConstraint(
            "check_status IN ('pending','quarantined','approved','rejected','deleted')",
            name="ck_attachment_check_status",
        ),
        CheckConstraint("size_bytes >= 0 AND size_bytes <= 10485760", name="ck_attachment_size_bytes"),
    )


# ---------------------------------------------------------------------------
# Import / ImportRowResult
# ---------------------------------------------------------------------------


class ImportBatchRow(Base):
    __tablename__ = "import_batch"
    id: Mapped[UUID] = mapped_column(PgUUID(as_uuid=True), primary_key=True)
    tenant_id: Mapped[UUID] = mapped_column(PgUUID(as_uuid=True), ForeignKey("tenant.id"), nullable=False)
    requested_by: Mapped[UUID] = mapped_column(PgUUID(as_uuid=True), ForeignKey("account.id"), nullable=False)
    dataset_kind: Mapped[str] = mapped_column(Text, nullable=False)
    source_attachment_id: Mapped[UUID | None] = mapped_column(PgUUID(as_uuid=True), ForeignKey("attachment.id"), nullable=True)
    mapping: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    scope: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    commit_request_id: Mapped[UUID | None] = mapped_column(PgUUID(as_uuid=True), nullable=True)
    counts: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        CheckConstraint(
            "status IN ('uploaded','validating','needs_correction','ready','committed','failed')",
            name="ck_import_batch_status",
        ),
    )


class ImportRowResultRow(Base):
    __tablename__ = "import_row_result"
    id: Mapped[UUID] = mapped_column(PgUUID(as_uuid=True), primary_key=True)
    tenant_id: Mapped[UUID] = mapped_column(PgUUID(as_uuid=True), ForeignKey("tenant.id"), nullable=False)
    import_batch_id: Mapped[UUID] = mapped_column(PgUUID(as_uuid=True), ForeignKey("import_batch.id"), nullable=False)
    row_no: Mapped[int] = mapped_column(Integer, nullable=False)
    row_fingerprint: Mapped[str] = mapped_column(Text, nullable=False)
    outcome: Mapped[str] = mapped_column(String(32), nullable=False)
    errors: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    target_ref: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    source_reference: Mapped[str | None] = mapped_column(Text, nullable=True)

    __table_args__ = (
        UniqueConstraint("import_batch_id", "row_no"),
        CheckConstraint(
            "outcome IN ('valid','invalid','duplicate_candidate','committed')",
            name="ck_import_row_outcome",
        ),
    )


# ---------------------------------------------------------------------------
# ExportJob
# ---------------------------------------------------------------------------


class ExportJobRow(Base):
    __tablename__ = "export_job"
    id: Mapped[UUID] = mapped_column(PgUUID(as_uuid=True), primary_key=True)
    tenant_id: Mapped[UUID] = mapped_column(PgUUID(as_uuid=True), ForeignKey("tenant.id"), nullable=False)
    requester_account_id: Mapped[UUID] = mapped_column(PgUUID(as_uuid=True), ForeignKey("account.id"), nullable=False)
    dataset_kind: Mapped[str] = mapped_column(Text, nullable=False)
    scope: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    filters: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    as_of: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    result_attachment_id: Mapped[UUID | None] = mapped_column(PgUUID(as_uuid=True), ForeignKey("attachment.id"), nullable=True)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        CheckConstraint(
            "status IN ('queued','running','ready','failed','cancelled','expired')",
            name="ck_export_job_status",
        ),
    )


# ---------------------------------------------------------------------------
# MetricDefinition
# ---------------------------------------------------------------------------


class MetricDefinitionRow(Base):
    __tablename__ = "metric_definition"
    id: Mapped[UUID] = mapped_column(PgUUID(as_uuid=True), primary_key=True)
    tenant_id: Mapped[UUID] = mapped_column(PgUUID(as_uuid=True), ForeignKey("tenant.id"), nullable=False)
    metric_code: Mapped[str] = mapped_column(Text, nullable=False)
    version_no: Mapped[int] = mapped_column(Integer, nullable=False)
    label: Mapped[str] = mapped_column(Text, nullable=False)
    source_spec: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    time_basis: Mapped[str] = mapped_column(String(32), nullable=False)
    # effective_range is a tstzrange; SQLAlchemy will marshal via psycopg/asyncpg.
    effective_range: Mapped[Any] = mapped_column(TSTZRANGE, nullable=False)
    approval: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        UniqueConstraint("tenant_id", "metric_code", "version_no"),
        CheckConstraint(
            "time_basis IN ('service_date','record_date','due_date')",
            name="ck_metric_definition_time_basis",
        ),
        CheckConstraint("version_no > 0", name="ck_metric_definition_version_positive"),
    )


# ---------------------------------------------------------------------------
# OperationalRun (NOT tenant-scoped per the data dictionary; RLS not enabled
# in 0009/0010).
# ---------------------------------------------------------------------------


class OperationalRunRow(Base):
    __tablename__ = "operational_run"
    id: Mapped[UUID] = mapped_column(PgUUID(as_uuid=True), primary_key=True)
    tenant_id: Mapped[UUID | None] = mapped_column(PgUUID(as_uuid=True), ForeignKey("tenant.id"), nullable=True)
    operation_kind: Mapped[str] = mapped_column(String(32), nullable=False)
    environment: Mapped[str] = mapped_column(String(32), nullable=False)
    execution_subject: Mapped[UUID] = mapped_column(PgUUID(as_uuid=True), ForeignKey("service_principal.id"), nullable=False)
    authorization_ref: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    evidence: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        CheckConstraint(
            "operation_kind IN ('backup','restore_test','restore','retention','reconciliation')",
            name="ck_op_run_kind",
        ),
        CheckConstraint(
            "environment IN ('development','test','production')",
            name="ck_op_run_environment",
        ),
        CheckConstraint(
            "status IN ('running','succeeded','failed','cancelled')",
            name="ck_op_run_status",
        ),
    )


# ---------------------------------------------------------------------------
# BackgroundJob (NOT tenant-scoped; nullable tenant_id).
# ---------------------------------------------------------------------------


class BackgroundJobRow(Base):
    __tablename__ = "background_job"
    id: Mapped[UUID] = mapped_column(PgUUID(as_uuid=True), primary_key=True)
    tenant_id: Mapped[UUID | None] = mapped_column(PgUUID(as_uuid=True), ForeignKey("tenant.id"), nullable=True)
    event_id: Mapped[UUID | None] = mapped_column(PgUUID(as_uuid=True), nullable=True)
    job_kind: Mapped[str] = mapped_column(Text, nullable=False)
    execution_subject: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    scope: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    result_ref: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        CheckConstraint(
            "status IN ('queued','running','succeeded','retry_wait','failed','cancelled')",
            name="ck_background_job_status",
        ),
        CheckConstraint("attempt_count >= 0", name="ck_background_job_attempts_nonneg"),
    )
