"""SQLAlchemy 2.x ORM models for the batch context."""
from __future__ import annotations

from datetime import date, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, TSTZRANGE, UUID as PgUUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class BatchRow(Base):
    __tablename__ = "batch"
    id: Mapped[UUID] = mapped_column(PgUUID(as_uuid=True), primary_key=True,
        server_default=text("ksma_uuid()"))
    tenant_id: Mapped[UUID] = mapped_column(PgUUID(as_uuid=True), ForeignKey("tenant.id"), nullable=False)
    unit_id: Mapped[UUID] = mapped_column(PgUUID(as_uuid=True), ForeignKey("education_unit.id"), nullable=False)
    batch_code: Mapped[str] = mapped_column(String(40), nullable=False)
    program_version_id: Mapped[UUID] = mapped_column(PgUUID(as_uuid=True), ForeignKey("program_version.id"), nullable=False)
    period_id: Mapped[UUID | None] = mapped_column(PgUUID(as_uuid=True), ForeignKey("academic_period.id"), nullable=True)
    capacity: Mapped[int] = mapped_column(Integer, nullable=False)
    delivery_mode: Mapped[str] = mapped_column(String(32), nullable=False)
    service_from: Mapped[date | None] = mapped_column(Date, nullable=True)
    service_until: Mapped[date | None] = mapped_column(Date, nullable=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=text("now()"))
    created_by: Mapped[str] = mapped_column(Text, nullable=False, default="system")
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=text("now()"))
    updated_by: Mapped[str] = mapped_column(Text, nullable=False, default="system")

    __table_args__ = (
        UniqueConstraint("unit_id", "batch_code"),
        CheckConstraint("capacity > 0", name="ck_batch_capacity_positive"),
    )


class ApplicationRow(Base):
    __tablename__ = "application"
    id: Mapped[UUID] = mapped_column(PgUUID(as_uuid=True), primary_key=True,
        server_default=text("ksma_uuid()"))
    tenant_id: Mapped[UUID] = mapped_column(PgUUID(as_uuid=True), ForeignKey("tenant.id"), nullable=False)
    unit_id: Mapped[UUID] = mapped_column(PgUUID(as_uuid=True), ForeignKey("education_unit.id"), nullable=False)
    participant_id: Mapped[UUID] = mapped_column(PgUUID(as_uuid=True), ForeignKey("participant.id"), nullable=False)
    program_id: Mapped[UUID] = mapped_column(PgUUID(as_uuid=True), ForeignKey("program.id"), nullable=False)
    requested_start: Mapped[date | None] = mapped_column(Date, nullable=True)
    completeness: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, nullable=False, default=list)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    decision: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    source_ref: Mapped[str | None] = mapped_column(String(120), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=text("now()"))
    created_by: Mapped[str] = mapped_column(Text, nullable=False, default="system")
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=text("now()"))
    updated_by: Mapped[str] = mapped_column(Text, nullable=False, default="system")


class EnrollmentRow(Base):
    __tablename__ = "enrollment"
    id: Mapped[UUID] = mapped_column(PgUUID(as_uuid=True), primary_key=True,
        server_default=text("ksma_uuid()"))
    tenant_id: Mapped[UUID] = mapped_column(PgUUID(as_uuid=True), ForeignKey("tenant.id"), nullable=False)
    unit_id: Mapped[UUID] = mapped_column(PgUUID(as_uuid=True), ForeignKey("education_unit.id"), nullable=False)
    participant_id: Mapped[UUID] = mapped_column(PgUUID(as_uuid=True), ForeignKey("participant.id"), nullable=False)
    batch_id: Mapped[UUID] = mapped_column(PgUUID(as_uuid=True), ForeignKey("batch.id"), nullable=False)
    application_id: Mapped[UUID | None] = mapped_column(PgUUID(as_uuid=True), ForeignKey("application.id"), nullable=True)
    effective_range: Mapped[Any] = mapped_column(TSTZRANGE, nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    policy_id: Mapped[UUID | None] = mapped_column(PgUUID(as_uuid=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=text("now()"))
    created_by: Mapped[str] = mapped_column(Text, nullable=False, default="system")
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=text("now()"))
    updated_by: Mapped[str] = mapped_column(Text, nullable=False, default="system")


class EnrollmentChangeRow(Base):
    __tablename__ = "enrollment_change"
    id: Mapped[UUID] = mapped_column(PgUUID(as_uuid=True), primary_key=True,
        server_default=text("ksma_uuid()"))
    tenant_id: Mapped[UUID] = mapped_column(PgUUID(as_uuid=True), ForeignKey("tenant.id"), nullable=False)
    source_enrollment_id: Mapped[UUID] = mapped_column(PgUUID(as_uuid=True), ForeignKey("enrollment.id"), nullable=False)
    target_enrollment_id: Mapped[UUID | None] = mapped_column(PgUUID(as_uuid=True), ForeignKey("enrollment.id"), nullable=True)
    change_kind: Mapped[str] = mapped_column(String(32), nullable=False)
    effective_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    decision: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=text("now()"))
    created_by: Mapped[str] = mapped_column(Text, nullable=False, default="system")
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=text("now()"))
    updated_by: Mapped[str] = mapped_column(Text, nullable=False, default="system")


class TeachingAssignmentRow(Base):
    __tablename__ = "teaching_assignment"
    id: Mapped[UUID] = mapped_column(PgUUID(as_uuid=True), primary_key=True,
        server_default=text("ksma_uuid()"))
    tenant_id: Mapped[UUID] = mapped_column(PgUUID(as_uuid=True), ForeignKey("tenant.id"), nullable=False)
    unit_id: Mapped[UUID] = mapped_column(PgUUID(as_uuid=True), ForeignKey("education_unit.id"), nullable=False)
    membership_id: Mapped[UUID] = mapped_column(PgUUID(as_uuid=True), ForeignKey("membership.id"), nullable=False)
    batch_id: Mapped[UUID | None] = mapped_column(PgUUID(as_uuid=True), ForeignKey("batch.id"), nullable=True)
    session_id: Mapped[UUID | None] = mapped_column(PgUUID(as_uuid=True), nullable=True)
    assignment_kind: Mapped[str] = mapped_column(String(32), nullable=False)
    component_codes: Mapped[list[str]] = mapped_column(ARRAY(String), nullable=False, default=list)
    valid_from: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    valid_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=text("now()"))
    created_by: Mapped[str] = mapped_column(Text, nullable=False, default="system")
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=text("now()"))
    updated_by: Mapped[str] = mapped_column(Text, nullable=False, default="system")

    __table_args__ = (
        CheckConstraint(
            "(batch_id IS NOT NULL AND session_id IS NULL) OR "
            "(batch_id IS NULL AND session_id IS NOT NULL)",
            name="ck_teaching_scope_xor",
        ),
    )