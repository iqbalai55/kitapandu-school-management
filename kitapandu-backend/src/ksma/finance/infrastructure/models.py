"""SQLAlchemy 2.x ORM models for the finance context.

Money columns are NUMERIC(18, 0) with CHECK (amount >= 0). Reversals use a
``reversal_total`` projection column on ``verified_payment`` that is updated by
``FinancialAdjustment(kind='payment_reversal')``.
"""
from __future__ import annotations

from datetime import date, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import (
    CheckConstraint,
    Date,
    DateTime,
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


class BillingPartyRow(Base):
    __tablename__ = "billing_party"
    id: Mapped[UUID] = mapped_column(PgUUID(as_uuid=True), primary_key=True)
    tenant_id: Mapped[UUID] = mapped_column(PgUUID(as_uuid=True), ForeignKey("tenant.id"), nullable=False)
    party_type: Mapped[str] = mapped_column(String(32), nullable=False)
    display_name: Mapped[str] = mapped_column(Text, nullable=False)
    contact: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    organization_ref: Mapped[str | None] = mapped_column(String(120), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_by: Mapped[str] = mapped_column(Text, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_by: Mapped[str] = mapped_column(Text, nullable=False)


class InvoiceRow(Base):
    __tablename__ = "invoice"
    id: Mapped[UUID] = mapped_column(PgUUID(as_uuid=True), primary_key=True)
    tenant_id: Mapped[UUID] = mapped_column(PgUUID(as_uuid=True), ForeignKey("tenant.id"), nullable=False)
    unit_id: Mapped[UUID] = mapped_column(PgUUID(as_uuid=True), ForeignKey("education_unit.id"), nullable=False)
    participant_id: Mapped[UUID] = mapped_column(PgUUID(as_uuid=True), ForeignKey("participant.id"), nullable=False)
    payer_party_id: Mapped[UUID | None] = mapped_column(PgUUID(as_uuid=True), ForeignKey("billing_party.id"), nullable=True)
    invoice_no: Mapped[str | None] = mapped_column(String(120), nullable=True)
    currency: Mapped[str] = mapped_column(String(8), nullable=False, default="IDR")
    document_status: Mapped[str] = mapped_column(String(32), nullable=False)
    issue_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    total_amount: Mapped[int] = mapped_column(Numeric(18, 0), nullable=False)
    policy_id: Mapped[UUID | None] = mapped_column(PgUUID(as_uuid=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_by: Mapped[str] = mapped_column(Text, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_by: Mapped[str] = mapped_column(Text, nullable=False)

    __table_args__ = (
        CheckConstraint(
            "document_status IN ('draft','issued','void')",
            name="ck_invoice_document_status",
        ),
        CheckConstraint("total_amount >= 0", name="ck_invoice_total_nonneg"),
        # invoice_no required when issued, unique within tenant
        UniqueConstraint("tenant_id", "invoice_no"),
    )


class InvoiceLineRow(Base):
    __tablename__ = "invoice_line"
    id: Mapped[UUID] = mapped_column(PgUUID(as_uuid=True), primary_key=True)
    tenant_id: Mapped[UUID] = mapped_column(PgUUID(as_uuid=True), ForeignKey("tenant.id"), nullable=False)
    invoice_id: Mapped[UUID] = mapped_column(PgUUID(as_uuid=True), ForeignKey("invoice.id"), nullable=False)
    line_no: Mapped[int] = mapped_column(Integer, nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    enrollment_id: Mapped[UUID | None] = mapped_column(PgUUID(as_uuid=True), ForeignKey("enrollment.id"), nullable=True)
    quantity: Mapped[int] = mapped_column(Integer, nullable=False)
    unit_price: Mapped[int] = mapped_column(Numeric(18, 0), nullable=False)
    discount_amount: Mapped[int] = mapped_column(Numeric(18, 0), nullable=False, default=0)
    net_amount: Mapped[int] = mapped_column(Numeric(18, 0), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_by: Mapped[str] = mapped_column(Text, nullable=False)

    __table_args__ = (
        UniqueConstraint("invoice_id", "line_no"),
        CheckConstraint("net_amount >= 0", name="ck_line_net_nonneg"),
        CheckConstraint("quantity > 0", name="ck_line_qty_positive"),
    )


class InstallmentRow(Base):
    __tablename__ = "installment"
    id: Mapped[UUID] = mapped_column(PgUUID(as_uuid=True), primary_key=True)
    tenant_id: Mapped[UUID] = mapped_column(PgUUID(as_uuid=True), ForeignKey("tenant.id"), nullable=False)
    invoice_id: Mapped[UUID] = mapped_column(PgUUID(as_uuid=True), ForeignKey("invoice.id"), nullable=False)
    sequence_no: Mapped[int] = mapped_column(Integer, nullable=False)
    due_date: Mapped[date] = mapped_column(Date, nullable=False)
    due_amount: Mapped[int] = mapped_column(Numeric(18, 0), nullable=False)
    timezone: Mapped[str] = mapped_column(String(64), nullable=False, default="Asia/Jakarta")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_by: Mapped[str] = mapped_column(Text, nullable=False)

    __table_args__ = (
        UniqueConstraint("invoice_id", "sequence_no"),
        CheckConstraint("due_amount > 0", name="ck_installment_amount_positive"),
    )


class PaymentSubmissionRow(Base):
    __tablename__ = "payment_submission"
    id: Mapped[UUID] = mapped_column(PgUUID(as_uuid=True), primary_key=True)
    tenant_id: Mapped[UUID] = mapped_column(PgUUID(as_uuid=True), ForeignKey("tenant.id"), nullable=False)
    unit_id: Mapped[UUID] = mapped_column(PgUUID(as_uuid=True), ForeignKey("education_unit.id"), nullable=False)
    participant_id: Mapped[UUID] = mapped_column(PgUUID(as_uuid=True), ForeignKey("participant.id"), nullable=False)
    invoice_id: Mapped[UUID | None] = mapped_column(PgUUID(as_uuid=True), ForeignKey("invoice.id"), nullable=True)
    submitted_by: Mapped[str] = mapped_column(Text, nullable=False)
    claimed_amount: Mapped[int] = mapped_column(Numeric(18, 0), nullable=False)
    currency: Mapped[str] = mapped_column(String(8), nullable=False, default="IDR")
    transfer_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    proof_attachment_id: Mapped[UUID | None] = mapped_column(PgUUID(as_uuid=True), nullable=True)
    transfer_reference: Mapped[str | None] = mapped_column(String(120), nullable=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    review_decision: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        CheckConstraint(
            "status IN ('submitted','under_review','verified','rejected')",
            name="ck_submission_status",
        ),
        CheckConstraint("claimed_amount > 0", name="ck_submission_amount_positive"),
    )


class VerifiedPaymentRow(Base):
    __tablename__ = "verified_payment"
    id: Mapped[UUID] = mapped_column(PgUUID(as_uuid=True), primary_key=True)
    tenant_id: Mapped[UUID] = mapped_column(PgUUID(as_uuid=True), ForeignKey("tenant.id"), nullable=False)
    unit_id: Mapped[UUID] = mapped_column(PgUUID(as_uuid=True), ForeignKey("education_unit.id"), nullable=False)
    submission_id: Mapped[UUID] = mapped_column(PgUUID(as_uuid=True), ForeignKey("payment_submission.id"), nullable=False)
    participant_id: Mapped[UUID] = mapped_column(PgUUID(as_uuid=True), ForeignKey("participant.id"), nullable=False)
    recognized_amount: Mapped[int] = mapped_column(Numeric(18, 0), nullable=False)
    currency: Mapped[str] = mapped_column(String(8), nullable=False, default="IDR")
    verification: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    received_date: Mapped[date] = mapped_column(Date, nullable=False)
    reversal_total: Mapped[int] = mapped_column(Numeric(18, 0), nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_by: Mapped[str] = mapped_column(Text, nullable=False)

    __table_args__ = (
        # R13: exactly one verified payment per submission
        UniqueConstraint("submission_id"),
        CheckConstraint("recognized_amount > 0", name="ck_vp_recognized_positive"),
        CheckConstraint("reversal_total >= 0", name="ck_vp_reversal_nonneg"),
    )


class PaymentAllocationRow(Base):
    __tablename__ = "payment_allocation"
    id: Mapped[UUID] = mapped_column(PgUUID(as_uuid=True), primary_key=True)
    tenant_id: Mapped[UUID] = mapped_column(PgUUID(as_uuid=True), ForeignKey("tenant.id"), nullable=False)
    payment_id: Mapped[UUID] = mapped_column(PgUUID(as_uuid=True), ForeignKey("verified_payment.id"), nullable=False)
    installment_id: Mapped[UUID] = mapped_column(PgUUID(as_uuid=True), ForeignKey("installment.id"), nullable=False)
    amount: Mapped[int] = mapped_column(Numeric(18, 0), nullable=False)
    allocated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    allocated_by: Mapped[str] = mapped_column(Text, nullable=False)
    idempotency_id: Mapped[UUID | None] = mapped_column(PgUUID(as_uuid=True), nullable=True)
    reversed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    reversal_reason: Mapped[str | None] = mapped_column(Text, nullable=True)

    __table_args__ = (
        CheckConstraint("amount > 0", name="ck_allocation_amount_positive"),
    )


class ReceiptRow(Base):
    __tablename__ = "receipt"
    id: Mapped[UUID] = mapped_column(PgUUID(as_uuid=True), primary_key=True)
    tenant_id: Mapped[UUID] = mapped_column(PgUUID(as_uuid=True), ForeignKey("tenant.id"), nullable=False)
    payment_id: Mapped[UUID] = mapped_column(PgUUID(as_uuid=True), ForeignKey("verified_payment.id"), nullable=False)
    receipt_no: Mapped[str] = mapped_column(String(120), nullable=False)
    snapshot: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    supersedes_id: Mapped[UUID | None] = mapped_column(PgUUID(as_uuid=True), ForeignKey("receipt.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_by: Mapped[str] = mapped_column(Text, nullable=False)

    __table_args__ = (
        UniqueConstraint("tenant_id", "receipt_no"),
        CheckConstraint(
            "status IN ('issued','replaced','void')",
            name="ck_receipt_status",
        ),
    )


class FinancialAdjustmentRow(Base):
    __tablename__ = "financial_adjustment"
    id: Mapped[UUID] = mapped_column(PgUUID(as_uuid=True), primary_key=True)
    tenant_id: Mapped[UUID] = mapped_column(PgUUID(as_uuid=True), ForeignKey("tenant.id"), nullable=False)
    adjustment_kind: Mapped[str] = mapped_column(String(32), nullable=False)
    source_kind: Mapped[str] = mapped_column(String(32), nullable=False)
    source_id: Mapped[UUID] = mapped_column(PgUUID(as_uuid=True), nullable=False)
    installment_id: Mapped[UUID | None] = mapped_column(PgUUID(as_uuid=True), ForeignKey("installment.id"), nullable=True)
    amount: Mapped[int] = mapped_column(Numeric(18, 0), nullable=False)
    decision: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    effective_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    idempotency_id: Mapped[UUID | None] = mapped_column(PgUUID(as_uuid=True), nullable=True)
    policy_id: Mapped[UUID | None] = mapped_column(PgUUID(as_uuid=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_by: Mapped[str] = mapped_column(Text, nullable=False)

    __table_args__ = (
        CheckConstraint(
            "adjustment_kind IN ('invoice_credit','allocation_reversal','payment_reversal')",
            name="ck_fa_kind",
        ),
        CheckConstraint("amount > 0", name="ck_fa_amount_positive"),
    )