"""SQLAlchemy 2.x ORM models for assessment + reporting."""
from __future__ import annotations

from datetime import date, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import (
    text,
    CheckConstraint,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, UUID as PgUUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


# --- Assessment --------------------------------------------------------------


class AssessmentRow(Base):
    __tablename__ = "assessment"
    id: Mapped[UUID] = mapped_column(PgUUID(as_uuid=True), primary_key=True,
        server_default=text("ksma_uuid()"))
    tenant_id: Mapped[UUID] = mapped_column(PgUUID(as_uuid=True), ForeignKey("tenant.id"), nullable=False)
    unit_id: Mapped[UUID] = mapped_column(PgUUID(as_uuid=True), ForeignKey("education_unit.id"), nullable=False)
    enrollment_id: Mapped[UUID] = mapped_column(PgUUID(as_uuid=True), ForeignKey("enrollment.id"), nullable=False)
    rubric_version_id: Mapped[UUID] = mapped_column(PgUUID(as_uuid=True), ForeignKey("rubric_version.id"), nullable=False)
    assessor_id: Mapped[UUID] = mapped_column(PgUUID(as_uuid=True), ForeignKey("membership.id"), nullable=False)
    assessment_date: Mapped[date] = mapped_column(Date, nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    supersedes_id: Mapped[UUID | None] = mapped_column(PgUUID(as_uuid=True), ForeignKey("assessment.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_by: Mapped[str] = mapped_column(Text, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_by: Mapped[str] = mapped_column(Text, nullable=False)

    __table_args__ = (
        CheckConstraint(
            "status IN ('draft','submitted','reviewed','superseded')",
            name="ck_assessment_status",
        ),
        CheckConstraint(
            "assessment_date <= CURRENT_DATE",
            name="ck_assessment_date_not_future",
        ),
    )


class AssessmentResultRow(Base):
    __tablename__ = "assessment_result"
    id: Mapped[UUID] = mapped_column(PgUUID(as_uuid=True), primary_key=True,
        server_default=text("ksma_uuid()"))
    tenant_id: Mapped[UUID] = mapped_column(PgUUID(as_uuid=True), ForeignKey("tenant.id"), nullable=False)
    assessment_id: Mapped[UUID] = mapped_column(PgUUID(as_uuid=True), ForeignKey("assessment.id"), nullable=False)
    indicator_id: Mapped[UUID] = mapped_column(PgUUID(as_uuid=True), ForeignKey("rubric_indicator.id"), nullable=False)
    numeric_value: Mapped[float | None] = mapped_column(Float, nullable=True)
    category_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    narrative_value: Mapped[str | None] = mapped_column(Text, nullable=True)
    result_state: Mapped[str] = mapped_column(String(32), nullable=False)
    follow_up: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_by: Mapped[str] = mapped_column(Text, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_by: Mapped[str] = mapped_column(Text, nullable=False)

    __table_args__ = (
        UniqueConstraint("assessment_id", "indicator_id"),
        CheckConstraint(
            "result_state IN ('not_assessed','assessed','not_applicable')",
            name="ck_result_state",
        ),
        # Exactly one of numeric/category/narrative is set per indicator value_kind.
        # The DB-level CHECK uses COALESCE-NULL-counting; indicator value_kind check
        # is enforced at the application layer (it knows the indicator).
        CheckConstraint(
            "(numeric_value IS NOT NULL)::int + (category_code IS NOT NULL)::int + "
            "(narrative_value IS NOT NULL)::int <= 1",
            name="ck_result_one_value_type",
        ),
    )


class AssessmentEvidenceRow(Base):
    __tablename__ = "assessment_evidence"
    id: Mapped[UUID] = mapped_column(PgUUID(as_uuid=True), primary_key=True,
        server_default=text("ksma_uuid()"))
    tenant_id: Mapped[UUID] = mapped_column(PgUUID(as_uuid=True), ForeignKey("tenant.id"), nullable=False)
    assessment_id: Mapped[UUID] = mapped_column(PgUUID(as_uuid=True), ForeignKey("assessment.id"), nullable=False)
    result_id: Mapped[UUID | None] = mapped_column(PgUUID(as_uuid=True), ForeignKey("assessment_result.id"), nullable=True)
    attachment_id: Mapped[UUID] = mapped_column(PgUUID(as_uuid=True), nullable=False)
    caption: Mapped[str | None] = mapped_column(Text, nullable=True)
    visibility: Mapped[str] = mapped_column(String(32), nullable=False, default="internal_assessment")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_by: Mapped[str] = mapped_column(Text, nullable=False)

    __table_args__ = (
        CheckConstraint(
            "visibility IN ('internal_assessment')",
            name="ck_evidence_visibility",
        ),
    )


# --- Reporting ---------------------------------------------------------------


class ReportPublicationRow(Base):
    __tablename__ = "report_publication"
    id: Mapped[UUID] = mapped_column(PgUUID(as_uuid=True), primary_key=True,
        server_default=text("ksma_uuid()"))
    tenant_id: Mapped[UUID] = mapped_column(PgUUID(as_uuid=True), ForeignKey("tenant.id"), nullable=False)
    unit_id: Mapped[UUID] = mapped_column(PgUUID(as_uuid=True), ForeignKey("education_unit.id"), nullable=False)
    enrollment_id: Mapped[UUID] = mapped_column(PgUUID(as_uuid=True), ForeignKey("enrollment.id"), nullable=False)
    publication_no: Mapped[int] = mapped_column(Integer, nullable=False)
    source_assessment_ids: Mapped[list[UUID]] = mapped_column(ARRAY(PgUUID(as_uuid=True)), nullable=False)
    snapshot: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    publication: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    attachment_id: Mapped[UUID | None] = mapped_column(PgUUID(as_uuid=True), nullable=True)
    supersedes_id: Mapped[UUID | None] = mapped_column(PgUUID(as_uuid=True), ForeignKey("report_publication.id"), nullable=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_by: Mapped[str] = mapped_column(Text, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_by: Mapped[str] = mapped_column(Text, nullable=False)

    __table_args__ = (
        UniqueConstraint("enrollment_id", "publication_no"),
        CheckConstraint(
            "status IN ('published','superseded','revoked')",
            name="ck_report_status",
        ),
    )


class CompletionDecisionRow(Base):
    __tablename__ = "completion_decision"
    id: Mapped[UUID] = mapped_column(PgUUID(as_uuid=True), primary_key=True,
        server_default=text("ksma_uuid()"))
    tenant_id: Mapped[UUID] = mapped_column(PgUUID(as_uuid=True), ForeignKey("tenant.id"), nullable=False)
    unit_id: Mapped[UUID] = mapped_column(PgUUID(as_uuid=True), ForeignKey("education_unit.id"), nullable=False)
    enrollment_id: Mapped[UUID] = mapped_column(PgUUID(as_uuid=True), ForeignKey("enrollment.id"), nullable=False)
    outcome: Mapped[str] = mapped_column(String(32), nullable=False)
    evidence_refs: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, nullable=False)
    policy_id: Mapped[UUID | None] = mapped_column(PgUUID(as_uuid=True), ForeignKey("policy_version.id"), nullable=True)
    decision: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    supersedes_id: Mapped[UUID | None] = mapped_column(PgUUID(as_uuid=True), ForeignKey("completion_decision.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_by: Mapped[str] = mapped_column(Text, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_by: Mapped[str] = mapped_column(Text, nullable=False)

    __table_args__ = (
        CheckConstraint(
            "outcome IN ('completed','remedial_required','not_completed')",
            name="ck_completion_outcome",
        ),
    )


class CertificateRow(Base):
    __tablename__ = "certificate"
    id: Mapped[UUID] = mapped_column(PgUUID(as_uuid=True), primary_key=True,
        server_default=text("ksma_uuid()"))
    tenant_id: Mapped[UUID] = mapped_column(PgUUID(as_uuid=True), ForeignKey("tenant.id"), nullable=False)
    unit_id: Mapped[UUID] = mapped_column(PgUUID(as_uuid=True), ForeignKey("education_unit.id"), nullable=False)
    completion_id: Mapped[UUID] = mapped_column(PgUUID(as_uuid=True), ForeignKey("completion_decision.id"), nullable=False)
    numbering_scope: Mapped[str] = mapped_column(String(80), nullable=False, default="default")
    certificate_no: Mapped[str] = mapped_column(String(120), nullable=False)
    snapshot: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    attachment_id: Mapped[UUID | None] = mapped_column(PgUUID(as_uuid=True), nullable=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    replacement_id: Mapped[UUID | None] = mapped_column(PgUUID(as_uuid=True), ForeignKey("certificate.id"), nullable=True)
    revocation: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_by: Mapped[str] = mapped_column(Text, nullable=False)

    __table_args__ = (
        UniqueConstraint("tenant_id", "numbering_scope", "certificate_no"),
        CheckConstraint(
            "status IN ('issued','replaced','revoked')",
            name="ck_certificate_status",
        ),
    )