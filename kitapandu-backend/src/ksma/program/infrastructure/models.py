"""SQLAlchemy 2.x ORM models for the program context."""
from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import (
    text,
    Boolean,
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID as PgUUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class ProgramRow(Base):
    __tablename__ = "program"
    id: Mapped[UUID] = mapped_column(PgUUID(as_uuid=True), primary_key=True,
        server_default=text("ksma_uuid()"))
    tenant_id: Mapped[UUID] = mapped_column(PgUUID(as_uuid=True), ForeignKey("tenant.id"), nullable=False)
    unit_id: Mapped[UUID] = mapped_column(PgUUID(as_uuid=True), ForeignKey("education_unit.id"), nullable=False)
    program_code: Mapped[str] = mapped_column(String(40), nullable=False)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    service_group: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    current_version_id: Mapped[UUID | None] = mapped_column(PgUUID(as_uuid=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=text("now()"))
    created_by: Mapped[str] = mapped_column(Text, nullable=False, default="system")
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=text("now()"))
    updated_by: Mapped[str] = mapped_column(Text, nullable=False, default="system")

    __table_args__ = (UniqueConstraint("unit_id", "program_code"),)


class ProgramVersionRow(Base):
    __tablename__ = "program_version"
    id: Mapped[UUID] = mapped_column(PgUUID(as_uuid=True), primary_key=True,
        server_default=text("ksma_uuid()"))
    tenant_id: Mapped[UUID] = mapped_column(PgUUID(as_uuid=True), ForeignKey("tenant.id"), nullable=False)
    unit_id: Mapped[UUID] = mapped_column(PgUUID(as_uuid=True), ForeignKey("education_unit.id"), nullable=False)
    program_id: Mapped[UUID] = mapped_column(PgUUID(as_uuid=True), ForeignKey("program.id"), nullable=False)
    version_no: Mapped[int] = mapped_column(Integer, nullable=False)
    learning_objectives: Mapped[str] = mapped_column(Text, nullable=False)
    curriculum: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    effective_from: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    effective_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=text("now()"))
    created_by: Mapped[str] = mapped_column(Text, nullable=False, default="system")
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=text("now()"))
    updated_by: Mapped[str] = mapped_column(Text, nullable=False, default="system")

    __table_args__ = (
        UniqueConstraint("program_id", "version_no"),
        CheckConstraint("version_no > 0", name="ck_version_no_positive"),
    )


class AcademicPeriodRow(Base):
    __tablename__ = "academic_period"
    id: Mapped[UUID] = mapped_column(PgUUID(as_uuid=True), primary_key=True,
        server_default=text("ksma_uuid()"))
    tenant_id: Mapped[UUID] = mapped_column(PgUUID(as_uuid=True), ForeignKey("tenant.id"), nullable=False)
    unit_id: Mapped[UUID] = mapped_column(PgUUID(as_uuid=True), ForeignKey("education_unit.id"), nullable=False)
    period_code: Mapped[str] = mapped_column(String(40), nullable=False)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    period_kind: Mapped[str] = mapped_column(String(32), nullable=False)
    start_date: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    end_date: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=text("now()"))
    created_by: Mapped[str] = mapped_column(Text, nullable=False, default="system")
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=text("now()"))
    updated_by: Mapped[str] = mapped_column(Text, nullable=False, default="system")

    __table_args__ = (
        UniqueConstraint("unit_id", "period_code"),
        CheckConstraint("end_date >= start_date", name="ck_period_end_after_start"),
    )


class RubricVersionRow(Base):
    __tablename__ = "rubric_version"
    id: Mapped[UUID] = mapped_column(PgUUID(as_uuid=True), primary_key=True,
        server_default=text("ksma_uuid()"))
    tenant_id: Mapped[UUID] = mapped_column(PgUUID(as_uuid=True), ForeignKey("tenant.id"), nullable=False)
    unit_id: Mapped[UUID] = mapped_column(PgUUID(as_uuid=True), ForeignKey("education_unit.id"), nullable=False)
    program_version_id: Mapped[UUID] = mapped_column(PgUUID(as_uuid=True), ForeignKey("program_version.id"), nullable=False)
    version_no: Mapped[int] = mapped_column(Integer, nullable=False)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    approval: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=text("now()"))
    created_by: Mapped[str] = mapped_column(Text, nullable=False, default="system")
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=text("now()"))
    updated_by: Mapped[str] = mapped_column(Text, nullable=False, default="system")

    __table_args__ = (
        UniqueConstraint("program_version_id", "version_no"),
        CheckConstraint("version_no > 0", name="ck_rubric_version_no_positive"),
    )


class RubricIndicatorRow(Base):
    __tablename__ = "rubric_indicator"
    id: Mapped[UUID] = mapped_column(PgUUID(as_uuid=True), primary_key=True,
        server_default=text("ksma_uuid()"))
    tenant_id: Mapped[UUID] = mapped_column(PgUUID(as_uuid=True), ForeignKey("tenant.id"), nullable=False)
    rubric_version_id: Mapped[UUID] = mapped_column(PgUUID(as_uuid=True), ForeignKey("rubric_version.id"), nullable=False)
    indicator_code: Mapped[str] = mapped_column(String(40), nullable=False)
    label: Mapped[str] = mapped_column(Text, nullable=False)
    value_kind: Mapped[str] = mapped_column(String(32), nullable=False)
    scale: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    weight: Mapped[float | None] = mapped_column(Numeric, nullable=True)
    is_required: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=text("now()"))
    created_by: Mapped[str] = mapped_column(Text, nullable=False, default="system")
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=text("now()"))
    updated_by: Mapped[str] = mapped_column(Text, nullable=False, default="system")

    __table_args__ = (UniqueConstraint("rubric_version_id", "indicator_code"),)