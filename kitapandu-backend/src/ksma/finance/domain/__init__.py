"""Finance domain entities + value objects."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from enum import StrEnum
from typing import Any
from uuid import UUID, uuid4


class InvoiceDocumentStatus(StrEnum):
    DRAFT = "draft"
    ISSUED = "issued"
    VOID = "void"


class InvoicePaymentStatus(StrEnum):
    """Projected — computed at query time, NOT stored."""

    UNPAID = "unpaid"
    PARTIAL = "partial"
    PAID = "paid"
    SETTLED_ZERO = "settled_zero"


class InvoiceDueStatus(StrEnum):
    NOT_DUE = "belum_jatuh_tempo"
    OVERDUE = "lewat_jatuh_tempo"


class PaymentSubmissionStatus(StrEnum):
    SUBMITTED = "submitted"
    UNDER_REVIEW = "under_review"
    VERIFIED = "verified"
    REJECTED = "rejected"


class AllocationReversalStatus(StrEnum):
    ACTIVE = "active"
    REVERSED = "reversed"


class ReceiptStatus(StrEnum):
    ISSUED = "issued"
    REPLACED = "replaced"
    VOID = "void"


class FinancialAdjustmentKind(StrEnum):
    INVOICE_CREDIT = "invoice_credit"
    ALLOCATION_REVERSAL = "allocation_reversal"
    PAYMENT_REVERSAL = "payment_reversal"


@dataclass(slots=True)
class BillingParty:
    id: UUID = field(default_factory=uuid4)
    tenant_id: UUID = field(default_factory=uuid4)
    party_type: str = "person"
    display_name: str = ""
    contact: dict[str, Any] | None = None
    organization_ref: str | None = None
    created_at: datetime | None = None
    created_by: str = ""
    updated_at: datetime | None = None
    updated_by: str = ""


@dataclass(slots=True)
class Invoice:
    id: UUID = field(default_factory=uuid4)
    tenant_id: UUID = field(default_factory=uuid4)
    unit_id: UUID = field(default_factory=uuid4)
    participant_id: UUID = field(default_factory=uuid4)
    payer_party_id: UUID | None = None
    invoice_no: str | None = None
    currency: str = "IDR"
    document_status: InvoiceDocumentStatus = InvoiceDocumentStatus.DRAFT
    issue_date: date | None = None
    total_amount: int = 0
    policy_id: UUID | None = None
    created_at: datetime | None = None
    created_by: str = ""
    updated_at: datetime | None = None
    updated_by: str = ""


@dataclass(slots=True)
class InvoiceLine:
    id: UUID = field(default_factory=uuid4)
    tenant_id: UUID = field(default_factory=uuid4)
    invoice_id: UUID = field(default_factory=uuid4)
    line_no: int = 0
    description: str = ""
    enrollment_id: UUID | None = None
    quantity: int = 1
    unit_price: int = 0
    discount_amount: int = 0
    net_amount: int = 0
    created_at: datetime | None = None
    created_by: str = ""


@dataclass(slots=True)
class Installment:
    id: UUID = field(default_factory=uuid4)
    tenant_id: UUID = field(default_factory=uuid4)
    invoice_id: UUID = field(default_factory=uuid4)
    sequence_no: int = 1
    due_date: date | None = None
    due_amount: int = 0
    timezone: str = "Asia/Jakarta"
    created_at: datetime | None = None
    created_by: str = ""


@dataclass(slots=True)
class PaymentSubmission:
    id: UUID = field(default_factory=uuid4)
    tenant_id: UUID = field(default_factory=uuid4)
    unit_id: UUID = field(default_factory=uuid4)
    participant_id: UUID = field(default_factory=uuid4)
    invoice_id: UUID | None = None
    submitted_by: str = ""
    claimed_amount: int = 0
    currency: str = "IDR"
    transfer_at: datetime | None = None
    proof_attachment_id: UUID | None = None
    transfer_reference: str | None = None
    status: PaymentSubmissionStatus = PaymentSubmissionStatus.SUBMITTED
    review_decision: dict[str, Any] | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None


@dataclass(slots=True)
class VerifiedPayment:
    """Funds recognized. `recognized_amount` is set ONCE; reversals decay via `reversal_total` projection."""

    id: UUID = field(default_factory=uuid4)
    tenant_id: UUID = field(default_factory=uuid4)
    unit_id: UUID = field(default_factory=uuid4)
    submission_id: UUID = field(default_factory=uuid4)
    participant_id: UUID = field(default_factory=uuid4)
    recognized_amount: int = 0
    currency: str = "IDR"
    verification: dict[str, Any] = field(default_factory=dict)
    received_date: date | None = None
    reversal_total: int = 0  # projection; updated by FinanceAdjustment(PAYMENT_REVERSAL)
    created_at: datetime | None = None
    created_by: str = ""


@dataclass(slots=True)
class PaymentAllocation:
    id: UUID = field(default_factory=uuid4)
    tenant_id: UUID = field(default_factory=uuid4)
    payment_id: UUID = field(default_factory=uuid4)
    installment_id: UUID = field(default_factory=uuid4)
    amount: int = 0
    allocated_at: datetime | None = None
    allocated_by: str = ""
    idempotency_id: UUID | None = None


@dataclass(slots=True)
class Receipt:
    id: UUID = field(default_factory=uuid4)
    tenant_id: UUID = field(default_factory=uuid4)
    payment_id: UUID = field(default_factory=uuid4)
    receipt_no: str = ""
    snapshot: dict[str, Any] = field(default_factory=dict)
    status: ReceiptStatus = ReceiptStatus.ISSUED
    supersedes_id: UUID | None = None
    created_at: datetime | None = None
    created_by: str = ""


@dataclass(slots=True)
class FinancialAdjustment:
    id: UUID = field(default_factory=uuid4)
    tenant_id: UUID = field(default_factory=uuid4)
    adjustment_kind: FinancialAdjustmentKind = FinancialAdjustmentKind.INVOICE_CREDIT
    source_ref: dict[str, Any] = field(default_factory=dict)
    installment_id: UUID | None = None
    amount: int = 0
    decision: dict[str, Any] = field(default_factory=dict)
    effective_at: datetime | None = None
    idempotency_id: UUID | None = None
    policy_id: UUID | None = None
    created_at: datetime | None = None
    created_by: str = ""