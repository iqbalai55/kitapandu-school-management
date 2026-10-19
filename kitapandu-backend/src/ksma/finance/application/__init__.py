"""Finance application services.

Showpiece: ``allocate_funds`` — atomic, idempotent, R14/R15/R21-compliant
allocation endpoint that maps directly to V13/V15/V16/V17/V18.

The transactional pattern:
    1. `SELECT … FOR UPDATE` on verified_payment + installment
    2. Compute available = recognized − reversal_total − sum(active allocations)
    3. Compute installment_remainder = due_amount − sum(active allocations) − invoice_credit
    4. Reject if amount > available OR amount > installment_remainder
    5. INSERT allocation
    6. COMMIT

Two simultaneous allocations of the same funds race safely via the FOR UPDATE
locks.
"""
from __future__ import annotations

from datetime import date, datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from ksma.finance.domain import (
    FinancialAdjustment,
    FinancialAdjustmentKind,
    Installment,
    Invoice,
    InvoiceDocumentStatus,
    InvoiceLine,
    PaymentAllocation,
    PaymentSubmission,
    PaymentSubmissionStatus,
    Receipt,
    ReceiptStatus,
    VerifiedPayment,
)
from ksma.finance.domain.events import (
    AllocationReversed,
    FundsAllocated,
    InvoiceCreated,
    InvoiceIssued,
    InvoiceVoided,
    PaymentRejected,
    PaymentSubmitted,
    PaymentVerified,
    PaymentReversed,
    ReceiptIssued,
)
from ksma.finance.infrastructure.repositories import (
    FinancialAdjustmentRepository,
    InstallmentRepository,
    InvoiceRepository,
    PaymentAllocationRepository,
    PaymentSubmissionRepository,
    ReceiptRepository,
    VerifiedPaymentRepository,
)
from ksma.shared.errors import (
    CurrencyMismatchError,
    DomainError,
    InsufficientFundsError,
    NegativeBalanceError,
    PolicyNotConfiguredError,
    StateConflictError,
    ValidationError,
)
from ksma.shared.events import bus
from ksma.shared.outbox import enqueue
from ksma.shared.tenancy import TenantContext
from ksma.shared.time import clock


# ---------------------------------------------------------------------------
# Policy gate
# ---------------------------------------------------------------------------


async def _require_published_finance_policy(
    session: AsyncSession, tenant_id: UUID
) -> UUID:
    """Return the published finance PolicyVersion id, or raise ``PolicyNotConfiguredError``."""
    row = (
        await session.execute(
            text(
                """
                SELECT id FROM policy_version
                WHERE tenant_id = :tid AND policy_domain = 'finance'
                  AND status = 'published'
                ORDER BY version_no DESC LIMIT 1
                """
            ),
            {"tid": str(tenant_id)},
        )
    ).first()
    if row is None:
        raise PolicyNotConfiguredError(
            "Finance flows require a published PolicyVersion with policy_domain='finance'",
            details={"decision": "Q04"},
        )
    return row.id


# ---------------------------------------------------------------------------
# Invoice
# ---------------------------------------------------------------------------


async def create_invoice(
    session: AsyncSession,
    ctx: TenantContext,
    *,
    unit_id: UUID,
    participant_id: UUID,
    payer_party_id: UUID | None = None,
    lines: list[dict[str, Any]],
    installments: list[dict[str, Any]] | None = None,
) -> Invoice:
    """Create a draft invoice + lines + (optionally) installments.

    Each line dict: ``{description, quantity, unit_price, discount_amount, enrollment_id?}``.
    Each installment dict: ``{sequence_no, due_date, due_amount}``.

    Sum of installment due_amounts must equal ``total_amount`` (R12).
    """
    policy_id = await _require_published_finance_policy(session, ctx.tenant_id)

    if not lines:
        raise ValidationError("At least one invoice line is required")
    total = 0
    for line in lines:
        qty = int(line["quantity"])
        price = int(line["unit_price"])
        disc = int(line.get("discount_amount", 0))
        if qty <= 0:
            raise ValidationError(f"quantity must be positive on line {line.get('description')}")
        if price < 0:
            raise ValidationError(f"unit_price must be non-negative on line {line.get('description')}")
        if disc < 0:
            raise ValidationError(f"discount_amount must be non-negative on line {line.get('description')}")
        net = qty * price - disc
        if net < 0:
            raise ValidationError(f"net_amount must be non-negative on line {line.get('description')}")
        total += net

    inst_total = sum(int(i["due_amount"]) for i in (installments or []))
    if installments:
        if inst_total != total:
            raise ValidationError(
                f"Installments sum {inst_total} must equal invoice total {total} (R12)"
            )

    inv = Invoice(
        tenant_id=ctx.tenant_id,
        unit_id=unit_id,
        participant_id=participant_id,
        payer_party_id=payer_party_id,
        currency="IDR",
        document_status=InvoiceDocumentStatus.DRAFT,
        total_amount=total,
        policy_id=policy_id,
        created_by=str(ctx.account_id),
        updated_by=str(ctx.account_id),
    )
    irepo = InvoiceRepository(session, ctx.tenant_id)
    await irepo.create(inv)

    # Lines
    for idx, line in enumerate(lines, start=1):
        net = int(line["quantity"]) * int(line["unit_price"]) - int(line.get("discount_amount", 0))
        await session.execute(
            text(
                """
                INSERT INTO invoice_line
                  (id, tenant_id, invoice_id, line_no, description,
                   enrollment_id, quantity, unit_price, discount_amount, net_amount,
                   created_at, created_by)
                VALUES
                  (ksma_uuid(), :tid, :iid, :ln, :desc, :eid,
                   :qty, :price, :disc, :net, now(), :cb)
                """
            ),
            {
                "tid": str(ctx.tenant_id),
                "iid": str(inv.id),
                "ln": idx,
                "desc": line["description"],
                "eid": str(line["enrollment_id"]) if line.get("enrollment_id") else None,
                "qty": int(line["quantity"]),
                "price": int(line["unit_price"]),
                "disc": int(line.get("discount_amount", 0)),
                "net": net,
                "cb": str(ctx.account_id),
            },
        )

    if installments:
        nrepo = InstallmentRepository(session, ctx.tenant_id)
        for inst in installments:
            i = Installment(
                tenant_id=ctx.tenant_id,
                invoice_id=inv.id,
                sequence_no=int(inst["sequence_no"]),
                due_date=inst["due_date"],
                due_amount=int(inst["due_amount"]),
                created_by=str(ctx.account_id),
            )
            await nrepo.create(i)

    event = InvoiceCreated(tenant_id=ctx.tenant_id, actor_id=ctx.account_id)
    await enqueue(session, event, aggregate_ref=f"invoice:{inv.id}")
    bus().dispatch(event)
    return inv


async def issue_invoice(
    session: AsyncSession, ctx: TenantContext, *, invoice_id: UUID
) -> Invoice:
    irepo = InvoiceRepository(session, ctx.tenant_id)
    inv = await irepo.lock_for_update(invoice_id)
    if inv is None:
        raise ValidationError("Invoice not found")
    if inv.document_status is not InvoiceDocumentStatus.DRAFT:
        raise StateConflictError(
            f"Cannot issue invoice in status {inv.document_status.value}"
        )
    invoice_no = await irepo.next_invoice_no()
    await irepo.set_status(
        invoice_id,
        status=InvoiceDocumentStatus.ISSUED,
        invoice_no=invoice_no,
        issue_date=clock().today(),
        actor=str(ctx.account_id),
    )
    inv.document_status = InvoiceDocumentStatus.ISSUED
    inv.invoice_no = invoice_no
    inv.issue_date = clock().today()
    event = InvoiceIssued(tenant_id=ctx.tenant_id, actor_id=ctx.account_id)
    await enqueue(session, event, aggregate_ref=f"invoice:{invoice_id}")
    bus().dispatch(event)
    return inv


async def void_invoice(
    session: AsyncSession, ctx: TenantContext, *, invoice_id: UUID, reason: str
) -> None:
    irepo = InvoiceRepository(session, ctx.tenant_id)
    inv = await irepo.lock_for_update(invoice_id)
    if inv is None:
        raise ValidationError("Invoice not found")
    if inv.document_status is InvoiceDocumentStatus.VOID:
        raise StateConflictError("Invoice is already void")
    allocated = await irepo.sum_active_allocations(invoice_id)
    if allocated > 0:
        raise StateConflictError(
            f"Cannot void invoice with active allocations ({allocated}); reverse allocations first",
            details={"active_allocations": allocated},
        )
    await irepo.set_status(
        invoice_id,
        status=InvoiceDocumentStatus.VOID,
        actor=str(ctx.account_id),
    )
    event = InvoiceVoided(tenant_id=ctx.tenant_id, actor_id=ctx.account_id)
    await enqueue(session, event, aggregate_ref=f"invoice:{invoice_id}")
    bus().dispatch(event)


# ---------------------------------------------------------------------------
# Payment Submission → Verification
# ---------------------------------------------------------------------------


async def submit_payment(
    session: AsyncSession,
    ctx: TenantContext,
    *,
    unit_id: UUID,
    participant_id: UUID,
    invoice_id: UUID | None,
    claimed_amount: int,
    transfer_at: datetime | None,
    proof_attachment_id: UUID | None,
    transfer_reference: str | None,
) -> PaymentSubmission:
    if claimed_amount <= 0:
        raise ValidationError("claimed_amount must be positive")
    srepo = PaymentSubmissionRepository(session, ctx.tenant_id)
    s = PaymentSubmission(
        tenant_id=ctx.tenant_id,
        unit_id=unit_id,
        participant_id=participant_id,
        invoice_id=invoice_id,
        submitted_by=str(ctx.account_id),
        claimed_amount=claimed_amount,
        currency="IDR",
        transfer_at=transfer_at,
        proof_attachment_id=proof_attachment_id,
        transfer_reference=transfer_reference,
        status=PaymentSubmissionStatus.SUBMITTED,
        review_decision=None,
    )
    await srepo.create(s)
    event = PaymentSubmitted(tenant_id=ctx.tenant_id, actor_id=ctx.account_id)
    await enqueue(session, event, aggregate_ref=f"payment_submission:{s.id}")
    bus().dispatch(event)
    return s


async def verify_payment(
    session: AsyncSession,
    ctx: TenantContext,
    *,
    submission_id: UUID,
    recognized_amount: int,
    reason: str,
) -> VerifiedPayment:
    """R13: verify + create VerifiedPayment in one transaction."""
    if recognized_amount <= 0:
        raise ValidationError("recognized_amount must be positive")
    srepo = PaymentSubmissionRepository(session, ctx.tenant_id)
    submission = await srepo.get(submission_id)
    if submission is None:
        raise ValidationError("PaymentSubmission not found")
    if submission.status is PaymentSubmissionStatus.VERIFIED:
        raise StateConflictError("Submission is already verified")
    if submission.status is PaymentSubmissionStatus.REJECTED:
        raise StateConflictError("Cannot verify a rejected submission")

    vrepo = VerifiedPaymentRepository(session, ctx.tenant_id)
    v = VerifiedPayment(
        tenant_id=ctx.tenant_id,
        unit_id=submission.unit_id,
        submission_id=submission_id,
        participant_id=submission.participant_id,
        recognized_amount=recognized_amount,
        currency=submission.currency,
        verification={
            "actor": str(ctx.account_id),
            "decided_at": clock().now().isoformat(),
            "reason": reason,
            "claimed": int(submission.claimed_amount),
            "delta_reason": "amount differs from claim" if recognized_amount != submission.claimed_amount else None,
        },
        received_date=clock().today(),
        created_by=str(ctx.account_id),
    )
    await vrepo.create(v)

    # Transition the submission in the same transaction.
    await srepo.update_status(
        submission_id,
        status=PaymentSubmissionStatus.VERIFIED,
        decision={
            "actor": str(ctx.account_id),
            "decided_at": clock().now().isoformat(),
            "recognized_amount": recognized_amount,
            "reason": reason,
        },
        actor=str(ctx.account_id),
    )

    event = PaymentVerified(tenant_id=ctx.tenant_id, actor_id=ctx.account_id)
    await enqueue(session, event, aggregate_ref=f"verified_payment:{v.id}")
    bus().dispatch(event)
    return v


async def reject_payment(
    session: AsyncSession,
    ctx: TenantContext,
    *,
    submission_id: UUID,
    reason: str,
) -> None:
    srepo = PaymentSubmissionRepository(session, ctx.tenant_id)
    submission = await srepo.get(submission_id)
    if submission is None:
        raise ValidationError("PaymentSubmission not found")
    if submission.status in {PaymentSubmissionStatus.VERIFIED, PaymentSubmissionStatus.REJECTED}:
        raise StateConflictError(
            f"Cannot reject submission in status {submission.status.value}"
        )
    await srepo.update_status(
        submission_id,
        status=PaymentSubmissionStatus.REJECTED,
        decision={
            "actor": str(ctx.account_id),
            "decided_at": clock().now().isoformat(),
            "reason": reason,
        },
        actor=str(ctx.account_id),
    )
    event = PaymentRejected(tenant_id=ctx.tenant_id, actor_id=ctx.account_id)
    await enqueue(session, event, aggregate_ref=f"payment_submission:{submission_id}")
    bus().dispatch(event)


# ---------------------------------------------------------------------------
# Allocation — the showpiece
# ---------------------------------------------------------------------------


async def allocate_funds(
    session: AsyncSession,
    ctx: TenantContext,
    *,
    payment_id: UUID,
    installment_id: UUID,
    amount: int,
    idempotency_id: UUID,
    issue_receipt: bool = True,
) -> tuple[PaymentAllocation, Receipt | None]:
    """FIN-A05 — allocate verified funds to an installment.

    Atomic: SELECT FOR UPDATE on payment + installment, computes available +
    remainder, rejects on overflow, INSERTs allocation, optionally issues receipt.

    The ``idempotency_id`` argument is the Idempotency-Key header value; the
    ``payment_allocation.idempotency_id`` column carries it for replay detection.
    """
    if amount <= 0:
        raise ValidationError("amount must be positive")

    vrepo = VerifiedPaymentRepository(session, ctx.tenant_id)
    nrepo = InstallmentRepository(session, ctx.tenant_id)

    vp = await vrepo.lock_for_update(payment_id)
    if vp is None:
        raise ValidationError("VerifiedPayment not found")
    inst = await nrepo.lock_for_update(installment_id)
    if inst is None:
        raise ValidationError("Installment not found")
    if vp.tenant_id != inst.tenant_id:
        raise ValidationError("Cross-tenant allocation is not allowed (BR01)")
    if vp.currency != "IDR":
        raise CurrencyMismatchError("Only IDR is supported in pilot")

    # R14 available = recognized - reversal_total - sum(active allocations)
    used = await vrepo.sum_active_allocations(payment_id)
    available = vp.recognized_amount - vp.reversal_total - used
    if available < amount:
        raise InsufficientFundsError(
            f"Allocation {amount} exceeds available funds {available}",
            details={"available": available, "requested": amount},
        )

    # R12 installment_remainder = due_amount - sum(active allocations) - invoice_credit
    inst_used = (
        await session.execute(
            text(
                """
                SELECT COALESCE(SUM(pa.amount), 0)::bigint AS total
                FROM payment_allocation pa
                WHERE pa.installment_id = :iid AND pa.tenant_id = :tid
                  AND pa.reversed_at IS NULL
                """
            ),
            {"iid": str(installment_id), "tid": str(ctx.tenant_id)},
        )
    ).first()
    credit = (
        await session.execute(
            text(
                """
                SELECT COALESCE(SUM(amount), 0)::bigint AS total
                FROM financial_adjustment
                WHERE installment_id = :iid AND tenant_id = :tid
                  AND adjustment_kind = 'invoice_credit'
                """
            ),
            {"iid": str(installment_id), "tid": str(ctx.tenant_id)},
        )
    ).first()
    remainder = inst.due_amount - int(inst_used.total or 0) - int(credit.total or 0)
    if remainder < amount:
        raise InsufficientFundsError(
            f"Allocation {amount} exceeds installment remainder {remainder}",
            details={"remainder": remainder, "requested": amount},
        )

    # Idempotency: if a row with this idempotency_id already exists for this actor+action,
    # return it without inserting again. (Caller has already verified uniqueness at HTTP layer.)
    existing = (
        await session.execute(
            text(
                """
                SELECT id, amount FROM payment_allocation
                WHERE tenant_id = :tid AND allocated_by = :actor
                  AND idempotency_id = :idem
                LIMIT 1
                """
            ),
            {"tid": str(ctx.tenant_id), "actor": str(ctx.account_id), "idem": str(idempotency_id)},
        )
    ).first()
    if existing is not None:
        alloc = await PaymentAllocationRepository(session, ctx.tenant_id).get(existing.id)
        return alloc, None  # type: ignore[return-value]

    alloc_repo = PaymentAllocationRepository(session, ctx.tenant_id)
    alloc = PaymentAllocation(
        tenant_id=ctx.tenant_id,
        payment_id=payment_id,
        installment_id=installment_id,
        amount=amount,
        allocated_at=clock().now(),
        allocated_by=str(ctx.account_id),
        idempotency_id=idempotency_id,
    )
    await alloc_repo.create(alloc)

    receipt: Receipt | None = None
    if issue_receipt:
        rrepo = ReceiptRepository(session, ctx.tenant_id)
        receipt = Receipt(
            tenant_id=ctx.tenant_id,
            payment_id=payment_id,
            receipt_no=await rrepo.next_receipt_no(),
            snapshot={
                "payment": {
                    "id": str(vp.id),
                    "recognized_amount": vp.recognized_amount,
                    "currency": vp.currency,
                    "received_date": vp.received_date.isoformat() if vp.received_date else None,
                },
                "allocations": [
                    {
                        "id": str(alloc.id),
                        "installment_id": str(installment_id),
                        "amount": amount,
                    }
                ],
                "issued_at": clock().now().isoformat(),
                "publisher": str(ctx.account_id),
            },
            status=ReceiptStatus.ISSUED,
            created_by=str(ctx.account_id),
        )
        await rrepo.create(receipt)

    event = FundsAllocated(tenant_id=ctx.tenant_id, actor_id=ctx.account_id)
    await enqueue(
        session,
        event,
        aggregate_ref=f"allocation:{alloc.id}",
    )
    bus().dispatch(event)
    if receipt is not None:
        revent = ReceiptIssued(tenant_id=ctx.tenant_id, actor_id=ctx.account_id)
        await enqueue(session, revent, aggregate_ref=f"receipt:{receipt.id}")
        bus().dispatch(revent)

    return alloc, receipt


# ---------------------------------------------------------------------------
# Reversal
# ---------------------------------------------------------------------------


async def reverse_allocation(
    session: AsyncSession,
    ctx: TenantContext,
    *,
    allocation_id: UUID,
    reason: str,
) -> FinancialAdjustment:
    if not reason:
        raise ValidationError("reason is required for allocation reversal")
    arepo = PaymentAllocationRepository(session, ctx.tenant_id)
    fa_repo = FinancialAdjustmentRepository(session, ctx.tenant_id)
    alloc = await arepo.get(allocation_id)
    if alloc is None:
        raise ValidationError("Allocation not found")

    await arepo.reverse(allocation_id, reason=reason, actor=str(ctx.account_id))

    fa = FinancialAdjustment(
        tenant_id=ctx.tenant_id,
        adjustment_kind=FinancialAdjustmentKind.ALLOCATION_REVERSAL,
        source_ref={"kind": "payment_allocation", "id": str(allocation_id)},
        amount=alloc.amount,
        decision={"actor": str(ctx.account_id), "reason": reason},
        effective_at=clock().now(),
        created_by=str(ctx.account_id),
    )
    await fa_repo.create(fa)

    event = AllocationReversed(tenant_id=ctx.tenant_id, actor_id=ctx.account_id)
    await enqueue(session, event, aggregate_ref=f"allocation:{allocation_id}")
    bus().dispatch(event)
    return fa


async def reverse_payment(
    session: AsyncSession,
    ctx: TenantContext,
    *,
    payment_id: UUID,
    reason: str,
) -> FinancialAdjustment:
    """R15: reversing a payment also reverses its active allocations atomically."""
    if not reason:
        raise ValidationError("reason is required for payment reversal")
    vrepo = VerifiedPaymentRepository(session, ctx.tenant_id)
    arepo = PaymentAllocationRepository(session, ctx.tenant_id)
    fa_repo = FinancialAdjustmentRepository(session, ctx.tenant_id)
    vp = await vrepo.lock_for_update(payment_id)
    if vp is None:
        raise ValidationError("VerifiedPayment not found")

    # Reverse all active allocations belonging to this payment, in one transaction.
    rows = (
        await session.execute(
            text(
                """
                SELECT id, amount FROM payment_allocation
                WHERE payment_id = :pid AND tenant_id = :tid
                  AND reversed_at IS NULL
                FOR UPDATE
                """
            ),
            {"pid": str(payment_id), "tid": str(ctx.tenant_id)},
        )
    ).all()
    total_reversed = 0
    for r in rows:
        await arepo.reverse(r.id, reason=reason, actor=str(ctx.account_id))
        await fa_repo.create(
            FinancialAdjustment(
                tenant_id=ctx.tenant_id,
                adjustment_kind=FinancialAdjustmentKind.ALLOCATION_REVERSAL,
                source_ref={"kind": "payment_allocation", "id": str(r.id)},
                amount=int(r.amount),
                decision={"actor": str(ctx.account_id), "reason": f"payment_reversal: {reason}"},
                effective_at=clock().now(),
                created_by=str(ctx.account_id),
            )
        )
        total_reversed += int(r.amount)

    await vrepo.bump_reversal_total(payment_id, total_reversed)

    fa = FinancialAdjustment(
        tenant_id=ctx.tenant_id,
        adjustment_kind=FinancialAdjustmentKind.PAYMENT_REVERSAL,
        source_ref={"kind": "verified_payment", "id": str(payment_id)},
        amount=total_reversed,
        decision={"actor": str(ctx.account_id), "reason": reason},
        effective_at=clock().now(),
        created_by=str(ctx.account_id),
    )
    await fa_repo.create(fa)

    event = PaymentReversed(tenant_id=ctx.tenant_id, actor_id=ctx.account_id)
    await enqueue(session, event, aggregate_ref=f"verified_payment:{payment_id}")
    bus().dispatch(event)
    return fa


# ---------------------------------------------------------------------------
# Invoice correction (invoice_credit)
# ---------------------------------------------------------------------------


async def invoice_credit(
    session: AsyncSession,
    ctx: TenantContext,
    *,
    installment_id: UUID,
    amount: int,
    reason: str,
) -> FinancialAdjustment:
    if amount <= 0:
        raise ValidationError("amount must be positive")
    nrepo = InstallmentRepository(session, ctx.tenant_id)
    fa_repo = FinancialAdjustmentRepository(session, ctx.tenant_id)
    inst = await nrepo.lock_for_update(installment_id)
    if inst is None:
        raise ValidationError("Installment not found")

    # Reject credit exceeding remainder.
    inst_used = (
        await session.execute(
            text(
                """
                SELECT COALESCE(SUM(pa.amount), 0)::bigint AS total
                FROM payment_allocation pa
                WHERE pa.installment_id = :iid AND pa.tenant_id = :tid
                  AND pa.reversed_at IS NULL
                """
            ),
            {"iid": str(installment_id), "tid": str(ctx.tenant_id)},
        )
    ).first()
    credit = (
        await session.execute(
            text(
                """
                SELECT COALESCE(SUM(amount), 0)::bigint AS total
                FROM financial_adjustment
                WHERE installment_id = :iid AND tenant_id = :tid
                  AND adjustment_kind = 'invoice_credit'
                """
            ),
            {"iid": str(installment_id), "tid": str(ctx.tenant_id)},
        )
    ).first()
    remainder = inst.due_amount - int(inst_used.total or 0) - int(credit.total or 0)
    if amount > remainder:
        raise ValidationError(
            f"Credit {amount} exceeds installment remainder {remainder}",
            details={"remainder": remainder, "requested": amount},
        )

    fa = FinancialAdjustment(
        tenant_id=ctx.tenant_id,
        adjustment_kind=FinancialAdjustmentKind.INVOICE_CREDIT,
        source_ref={"kind": "installment", "id": str(installment_id)},
        installment_id=installment_id,
        amount=amount,
        decision={"actor": str(ctx.account_id), "reason": reason},
        effective_at=clock().now(),
        created_by=str(ctx.account_id),
    )
    await fa_repo.create(fa)
    return fa


# ---------------------------------------------------------------------------
# Projection query (paid / partial / overdue / settled_zero)
# ---------------------------------------------------------------------------


async def compute_invoice_projection(
    session: AsyncSession, ctx: TenantContext, invoice_id: UUID
) -> dict[str, Any]:
    irepo = InvoiceRepository(session, ctx.tenant_id)
    inv = await irepo.get(invoice_id)
    if inv is None:
        raise ValidationError("Invoice not found")
    allocated = await irepo.sum_active_allocations(invoice_id)
    today = clock().today()
    rows = await session.execute(
        text(
            """
            SELECT i.id, i.due_date, i.due_amount,
                   COALESCE(SUM(pa.amount) FILTER (WHERE pa.reversed_at IS NULL), 0)::bigint AS allocated,
                   COALESCE(SUM(fa.amount) FILTER (WHERE fa.adjustment_kind = 'invoice_credit'), 0)::bigint AS credited
            FROM installment i
            LEFT JOIN payment_allocation pa ON pa.installment_id = i.id
            LEFT JOIN financial_adjustment fa ON fa.installment_id = i.id
            WHERE i.invoice_id = :iid AND i.tenant_id = :tid
            GROUP BY i.id, i.due_date, i.due_amount
            ORDER BY i.sequence_no
            """
        ),
        {"iid": str(invoice_id), "tid": str(ctx.tenant_id)},
    )
    installments = []
    overdue_amount = 0
    for r in rows:
        effective = int(r.due_amount) - int(r.credited)
        remainder = effective - int(r.allocated)
        due_status = (
            InvoiceDocumentStatus.__class__  # placeholder
            and (
                "overdue" if r.due_date < today and remainder > 0 else "not_due"
            )
        )
        if due_status == "overdue":
            overdue_amount += remainder
        installments.append(
            {
                "installment_id": str(r.id),
                "due_date": r.due_date.isoformat(),
                "due_amount": int(r.due_amount),
                "credited": int(r.credited),
                "allocated": int(r.allocated),
                "remainder": remainder,
                "due_status": due_status,
            }
        )
    if inv.total_amount == 0 or (allocated + overdue_amount) == 0:
        payment = "settled_zero"
    elif allocated >= inv.total_amount:
        payment = "paid"
    elif allocated > 0:
        payment = "partial"
    else:
        payment = "unpaid"
    return {
        "invoice_id": str(invoice_id),
        "total_amount": inv.total_amount,
        "document_status": inv.document_status.value,
        "payment_status": payment,
        "active_allocation_total": allocated,
        "overdue_amount": overdue_amount,
        "installments": installments,
    }