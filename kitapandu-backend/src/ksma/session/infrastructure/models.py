"""SQLAlchemy 2.x ORM models for the session context."""
from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import (
    text,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID as PgUUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class SessionRow(Base):
    __tablename__ = "session"
    id: Mapped[UUID] = mapped_column(PgUUID(as_uuid=True), primary_key=True,
        server_default=text("ksma_uuid()"))
    tenant_id: Mapped[UUID] = mapped_column(PgUUID(as_uuid=True), ForeignKey("tenant.id"), nullable=False)
    unit_id: Mapped[UUID] = mapped_column(PgUUID(as_uuid=True), ForeignKey("education_unit.id"), nullable=False)
    batch_id: Mapped[UUID] = mapped_column(PgUUID(as_uuid=True), ForeignKey("batch.id"), nullable=False)
    session_no: Mapped[int] = mapped_column(Integer, nullable=False)
    effective_revision_id: Mapped[UUID | None] = mapped_column(PgUUID(as_uuid=True), nullable=True)
    lifecycle_status: Mapped[str] = mapped_column(String(32), nullable=False)
    closure_id: Mapped[UUID | None] = mapped_column(PgUUID(as_uuid=True), nullable=True)
    reschedule_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=text("now()"))
    created_by: Mapped[str] = mapped_column(Text, nullable=False, default="system")
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=text("now()"))
    updated_by: Mapped[str] = mapped_column(Text, nullable=False, default="system")

    __table_args__ = (
        UniqueConstraint("batch_id", "session_no"),
        CheckConstraint("session_no > 0", name="ck_session_no_positive"),
    )


class SessionRevisionRow(Base):
    __tablename__ = "session_revision"
    id: Mapped[UUID] = mapped_column(PgUUID(as_uuid=True), primary_key=True,
        server_default=text("ksma_uuid()"))
    tenant_id: Mapped[UUID] = mapped_column(PgUUID(as_uuid=True), ForeignKey("tenant.id"), nullable=False)
    session_id: Mapped[UUID] = mapped_column(PgUUID(as_uuid=True), ForeignKey("session.id"), nullable=False)
    revision_no: Mapped[int] = mapped_column(Integer, nullable=False)
    starts_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    ends_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    timezone: Mapped[str] = mapped_column(String(64), nullable=False)
    location: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    change_reason: Mapped[str] = mapped_column(Text, nullable=False)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=text("now()"))
    created_by: Mapped[str] = mapped_column(Text, nullable=False, default="system")
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=text("now()"))
    updated_by: Mapped[str] = mapped_column(Text, nullable=False, default="system")

    __table_args__ = (
        UniqueConstraint("session_id", "revision_no"),
        CheckConstraint("revision_no > 0", name="ck_revision_no_positive"),
        CheckConstraint("ends_at > starts_at", name="ck_revision_ends_after_starts"),
    )


class AttendanceRow(Base):
    __tablename__ = "attendance"
    id: Mapped[UUID] = mapped_column(PgUUID(as_uuid=True), primary_key=True,
        server_default=text("ksma_uuid()"))
    tenant_id: Mapped[UUID] = mapped_column(PgUUID(as_uuid=True), ForeignKey("tenant.id"), nullable=False)
    session_id: Mapped[UUID] = mapped_column(PgUUID(as_uuid=True), ForeignKey("session.id"), nullable=False)
    enrollment_id: Mapped[UUID] = mapped_column(PgUUID(as_uuid=True), ForeignKey("enrollment.id"), nullable=False)
    attendance_code: Mapped[str | None] = mapped_column(String(32), nullable=True)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    recorded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    recorded_by: Mapped[str | None] = mapped_column(Text, nullable=True)
    correction_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=text("now()"))
    created_by: Mapped[str] = mapped_column(Text, nullable=False, default="system")
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=text("now()"))
    updated_by: Mapped[str] = mapped_column(Text, nullable=False, default="system")

    __table_args__ = (
        # R09: exactly one effective attendance per (session, enrollment)
        UniqueConstraint("session_id", "enrollment_id", name="uq_attendance_session_enrollment"),
        CheckConstraint(
            "attendance_code IS NULL OR attendance_code IN "
            "('hadir','izin','sakit','tidak_hadir','terlambat')",
            name="ck_attendance_code_enum",
        ),
    )


class TeachingJournalRow(Base):
    __tablename__ = "teaching_journal"
    id: Mapped[UUID] = mapped_column(PgUUID(as_uuid=True), primary_key=True,
        server_default=text("ksma_uuid()"))
    tenant_id: Mapped[UUID] = mapped_column(PgUUID(as_uuid=True), ForeignKey("tenant.id"), nullable=False)
    session_id: Mapped[UUID] = mapped_column(PgUUID(as_uuid=True), ForeignKey("session.id"), nullable=False)
    actual_instructor_id: Mapped[UUID] = mapped_column(PgUUID(as_uuid=True), ForeignKey("membership.id"), nullable=False)
    topic: Mapped[str] = mapped_column(Text, nullable=False)
    activities: Mapped[str | None] = mapped_column(Text, nullable=True)
    internal_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    portal_summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    follow_up: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=text("now()"))
    created_by: Mapped[str] = mapped_column(Text, nullable=False, default="system")
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=text("now()"))
    updated_by: Mapped[str] = mapped_column(Text, nullable=False, default="system")

    __table_args__ = (UniqueConstraint("session_id"),)


class SessionCompletionRow(Base):
    __tablename__ = "session_completion"
    id: Mapped[UUID] = mapped_column(PgUUID(as_uuid=True), primary_key=True,
        server_default=text("ksma_uuid()"))
    tenant_id: Mapped[UUID] = mapped_column(PgUUID(as_uuid=True), ForeignKey("tenant.id"), nullable=False)
    session_id: Mapped[UUID] = mapped_column(PgUUID(as_uuid=True), ForeignKey("session.id"), nullable=False)
    completion_no: Mapped[int] = mapped_column(Integer, nullable=False)
    decision: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    checklist_snapshot: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    policy_id: Mapped[UUID | None] = mapped_column(PgUUID(as_uuid=True), nullable=True)
    supersedes_id: Mapped[UUID | None] = mapped_column(PgUUID(as_uuid=True), ForeignKey("session_completion.id"), nullable=True)
    correction_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    outcome: Mapped[str] = mapped_column(String(32), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=text("now()"))
    created_by: Mapped[str] = mapped_column(Text, nullable=False, default="system")
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=text("now()"))
    updated_by: Mapped[str] = mapped_column(Text, nullable=False, default="system")

    __table_args__ = (UniqueConstraint("session_id", "completion_no"),)