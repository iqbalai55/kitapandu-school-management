"""Finance FastAPI router."""
from __future__ import annotations

from datetime import date, datetime
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Header, Query, status
from pydantic import BaseModel, ConfigDict, Field

from ksma.finance.application import (
    allocate_funds,
    compute_invoice_projection,
    create_invoice,
    issue_invoice,
    invoice_credit,
    reject_payment,
    reverse_allocation,
    reverse_payment,
    submit_payment,
    verify_payment,
    void_invoice,
)
from ksma.finance.domain import (
    InvoiceDocumentStatus,
    PaymentSubmissionStatus,
)
from ksma.finance.infrastructure.repositories import (
    InvoiceRepository,
    ReceiptRepository,
    VerifiedPaymentRepository,
)
from ksma.shared.persistence import session_scope
from ksma.shared.tenancy import TenantContext

router = APIRouter(prefix="/api/v1", tags=["finance"])


def require_tenant_context() -> TenantContext:  # pragma: no cover
    raise NotImplementedError


# --- Schemas -----------------------------------------------------------------


class InvoiceLineIn(BaseModel):
    description: str = Field(min_length=1, max_length=200)
    enrollment_id: UUID | None = None
    quantity: int = Field(gt=0)
    unit_price: int = Field(ge=0)
    discount_amount: int = Field(default=0, ge=0)


class InstallmentIn(BaseModel):
    sequence_no: int = Field(ge=1)
    due_date: date
    due_amount: int = Field(gt=0)


class CreateInvoiceBody(BaseModel):
    unit_id: UUID
    participant_id: UUID
    payer_party_id: UUID | None = None
    lines: list[InvoiceLineIn] = Field(min_length=1)
    installments: list[InstallmentIn] | None = None


class InvoiceOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    unit_id: UUID
    participant_id: UUID
    payer_party_id: UUID | None = None
    invoice_no: str | None = None
    currency: str
    document_status: str
    issue_date: date | None = None
    total_amount: int


class InvoiceStatusOut(BaseModel):
    invoice_id: UUID
    total_amount: int
    document_status: str
    payment_status: str
    active_allocation_total: int
    overdue_amount: int
    installments: list[dict[str, Any]]


class SubmitPaymentBody(BaseModel):
    unit_id: UUID
    participant_id: UUID
    invoice_id: UUID | None = None
    claimed_amount: int = Field(gt=0)
    transfer_at: datetime | None = None
    proof_attachment_id: UUID | None = None
    transfer_reference: str | None = Field(default=None, max_length=120)


class PaymentSubmissionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    participant_id: UUID
    invoice_id: UUID | None = None
    claimed_amount: int
    currency: str
    status: str
    review_decision: dict[str, Any] | None = None


class VerifyPaymentBody(BaseModel):
    recognized_amount: int = Field(gt=0)
    reason: str = Field(min_length=1, max_length=1000)


class RejectPaymentBody(BaseModel):
    reason: str = Field(min_length=1, max_length=1000)


class VerifiedPaymentOut(BaseModel):
    id: UUID
    participant_id: UUID
    submission_id: UUID
    recognized_amount: int
    currency: str
    received_date: date


class AllocationBody(BaseModel):
    payment_id: UUID
    installment_id: UUID
    amount: int = Field(gt=0)
    issue_receipt: bool = True


class AllocationOut(BaseModel):
    id: UUID
    payment_id: UUID
    installment_id: UUID
    amount: int
    allocated_at: datetime | None = None
    receipt_id: UUID | None = None
    receipt_no: str | None = None


class ReverseAllocationBody(BaseModel):
    reason: str = Field(min_length=1, max_length=1000)


class ReversePaymentBody(BaseModel):
    reason: str = Field(min_length=1, max_length=1000)


class InvoiceCreditBody(BaseModel):
    installment_id: UUID
    amount: int = Field(gt=0)
    reason: str = Field(min_length=1, max_length=1000)


class ReceiptOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    payment_id: UUID
    receipt_no: str
    snapshot: dict[str, Any]
    status: str


# --- Invoice endpoints -------------------------------------------------------


@router.post("/invoices", response_model=InvoiceOut, status_code=status.HTTP_201_CREATED)
async def post_invoice(
    body: CreateInvoiceBody,
    ctx: TenantContext = Depends(require_tenant_context),
) -> InvoiceOut:
    async with session_scope() as session:
        inv = await create_invoice(
            session,
            ctx,
            unit_id=body.unit_id,
            participant_id=body.participant_id,
            payer_party_id=body.payer_party_id,
            lines=[l.model_dump() for l in body.lines],
            installments=[i.model_dump() for i in body.installments] if body.installments else None,
        )
        return InvoiceOut(
            id=inv.id,
            unit_id=inv.unit_id,
            participant_id=inv.participant_id,
            payer_party_id=inv.payer_party_id,
            invoice_no=inv.invoice_no,
            currency=inv.currency,
            document_status=inv.document_status.value,
            issue_date=inv.issue_date,
            total_amount=inv.total_amount,
        )


@router.post("/invoices/{invoice_id}/issue", response_model=InvoiceOut)
async def post_issue_invoice(
    invoice_id: UUID,
    ctx: TenantContext = Depends(require_tenant_context),
) -> InvoiceOut:
    async with session_scope() as session:
        inv = await issue_invoice(session, ctx, invoice_id=invoice_id)
        return InvoiceOut(
            id=inv.id,
            unit_id=inv.unit_id,
            participant_id=inv.participant_id,
            payer_party_id=inv.payer_party_id,
            invoice_no=inv.invoice_no,
            currency=inv.currency,
            document_status=inv.document_status.value,
            issue_date=inv.issue_date,
            total_amount=inv.total_amount,
        )


@router.post("/invoices/{invoice_id}/void", status_code=status.HTTP_204_NO_CONTENT)
async def post_void_invoice(
    invoice_id: UUID,
    reason: str = Query(min_length=1, max_length=1000),
    ctx: TenantContext = Depends(require_tenant_context),
) -> None:
    async with session_scope() as session:
        await void_invoice(session, ctx, invoice_id=invoice_id, reason=reason)


@router.post("/invoices/{invoice_id}/credits", status_code=status.HTTP_204_NO_CONTENT)
async def post_invoice_credit(
    invoice_id: UUID,
    body: InvoiceCreditBody,
    ctx: TenantContext = Depends(require_tenant_context),
) -> None:
    async with session_scope() as session:
        await invoice_credit(
            session,
            ctx,
            installment_id=body.installment_id,
            amount=body.amount,
            reason=body.reason,
        )


@router.get("/invoices/{invoice_id}", response_model=InvoiceOut)
async def get_invoice(
    invoice_id: UUID,
    ctx: TenantContext = Depends(require_tenant_context),
) -> InvoiceOut:
    async with session_scope() as session:
        repo = InvoiceRepository(session, ctx.tenant_id)
        inv = await repo.get(invoice_id)
        if inv is None:
            raise HTTPException(status_code=404, detail="not found")
        return InvoiceOut(
            id=inv.id,
            unit_id=inv.unit_id,
            participant_id=inv.participant_id,
            payer_party_id=inv.payer_party_id,
            invoice_no=inv.invoice_no,
            currency=inv.currency,
            document_status=inv.document_status.value,
            issue_date=inv.issue_date,
            total_amount=inv.total_amount,
        )


@router.get("/invoices/{invoice_id}/status", response_model=InvoiceStatusOut)
async def get_invoice_status(
    invoice_id: UUID,
    ctx: TenantContext = Depends(require_tenant_context),
) -> InvoiceStatusOut:
    async with session_scope() as session:
        projection = await compute_invoice_projection(session, ctx, invoice_id)
        return InvoiceStatusOut(**projection)


# --- Payment endpoints -------------------------------------------------------


@router.post(
    "/payments",
    response_model=PaymentSubmissionOut,
    status_code=status.HTTP_201_CREATED,
)
async def post_payment(
    body: SubmitPaymentBody,
    ctx: TenantContext = Depends(require_tenant_context),
) -> PaymentSubmissionOut:
    async with session_scope() as session:
        s = await submit_payment(
            session,
            ctx,
            unit_id=body.unit_id,
            participant_id=body.participant_id,
            invoice_id=body.invoice_id,
            claimed_amount=body.claimed_amount,
            transfer_at=body.transfer_at,
            proof_attachment_id=body.proof_attachment_id,
            transfer_reference=body.transfer_reference,
        )
        return PaymentSubmissionOut(
            id=s.id,
            participant_id=s.participant_id,
            invoice_id=s.invoice_id,
            claimed_amount=s.claimed_amount,
            currency=s.currency,
            status=s.status.value,
        )


@router.post("/payments/{submission_id}/verify", response_model=VerifiedPaymentOut)
async def post_verify_payment(
    submission_id: UUID,
    body: VerifyPaymentBody,
    ctx: TenantContext = Depends(require_tenant_context),
) -> VerifiedPaymentOut:
    async with session_scope() as session:
        v = await verify_payment(
            session,
            ctx,
            submission_id=submission_id,
            recognized_amount=body.recognized_amount,
            reason=body.reason,
        )
        return VerifiedPaymentOut(
            id=v.id,
            participant_id=v.participant_id,
            submission_id=v.submission_id,
            recognized_amount=v.recognized_amount,
            currency=v.currency,
            received_date=v.received_date,
        )


@router.post("/payments/{submission_id}/reject", status_code=status.HTTP_204_NO_CONTENT)
async def post_reject_payment(
    submission_id: UUID,
    body: RejectPaymentBody,
    ctx: TenantContext = Depends(require_tenant_context),
) -> None:
    async with session_scope() as session:
        await reject_payment(session, ctx, submission_id=submission_id, reason=body.reason)


@router.post(
    "/payments/{payment_id}/reverse",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def post_reverse_payment(
    payment_id: UUID,
    body: ReversePaymentBody,
    ctx: TenantContext = Depends(require_tenant_context),
) -> None:
    async with session_scope() as session:
        await reverse_payment(session, ctx, payment_id=payment_id, reason=body.reason)


# --- Allocation endpoints ----------------------------------------------------


@router.post("/allocations", response_model=AllocationOut)
async def post_allocation(
    body: AllocationBody,
    idempotency_key: str = Header(..., alias="Idempotency-Key", min_length=8, max_length=200),
    ctx: TenantContext = Depends(require_tenant_context),
) -> AllocationOut:
    """FIN-A05 — allocate funds to an installment. Atomic + idempotent."""
    import hashlib

    key_fp = hashlib.sha256(idempotency_key.encode()).hexdigest()
    async with session_scope() as session:
        alloc, receipt = await allocate_funds(
            session,
            ctx,
            payment_id=body.payment_id,
            installment_id=body.installment_id,
            amount=body.amount,
            idempotency_id=UUID(hex=key_fp[:32]),
            issue_receipt=body.issue_receipt,
        )
        return AllocationOut(
            id=alloc.id,
            payment_id=alloc.payment_id,
            installment_id=alloc.installment_id,
            amount=alloc.amount,
            allocated_at=alloc.allocated_at,
            receipt_id=receipt.id if receipt else None,
            receipt_no=receipt.receipt_no if receipt else None,
        )


@router.post(
    "/allocations/{allocation_id}/reverse",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def post_reverse_allocation(
    allocation_id: UUID,
    body: ReverseAllocationBody,
    ctx: TenantContext = Depends(require_tenant_context),
) -> None:
    async with session_scope() as session:
        await reverse_allocation(
            session,
            ctx,
            allocation_id=allocation_id,
            reason=body.reason,
        )


# --- Receipt endpoint --------------------------------------------------------


@router.get("/receipts/{receipt_id}", response_model=ReceiptOut)
async def get_receipt(
    receipt_id: UUID,
    ctx: TenantContext = Depends(require_tenant_context),
) -> ReceiptOut:
    async with session_scope() as session:
        repo = ReceiptRepository(session, ctx.tenant_id)
        r = await repo.get(receipt_id)
        if r is None:
            raise HTTPException(status_code=404, detail="not found")
        return ReceiptOut(
            id=r.id,
            payment_id=r.payment_id,
            receipt_no=r.receipt_no,
            snapshot=r.snapshot,
            status=r.status.value,
        )