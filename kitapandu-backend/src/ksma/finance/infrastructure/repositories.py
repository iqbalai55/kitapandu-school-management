"""Finance repositories — tenant-scoped."""
from __future__ import annotations

from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from ksma.finance.domain import (
    BillingParty,
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


def _json(d: Any) -> str:
    import json

    return json.dumps(d, default=str)


# ---------------------------------------------------------------------------
# BillingParty
# ---------------------------------------------------------------------------


class BillingPartyRepository:
    def __init__(self, session: AsyncSession, tenant_id: UUID) -> None:
        self.session = session
        self.tenant_id = tenant_id

    async def create(self, p: BillingParty) -> None:
        await self.session.execute(
            text(
                """
                INSERT INTO billing_party
                  (id, tenant_id, party_type, display_name, contact, organization_ref,
                   created_at, created_by, updated_at, updated_by)
                VALUES
                  (:id, :tid, :pt, :dn, CAST(:contact AS JSONB), :oref,
                   now(), :cb, now(), :ub)
                """
            ),
            {
                "id": str(p.id),
                "tid": str(self.tenant_id),
                "pt": p.party_type,
                "dn": p.display_name,
                "contact": _json(p.contact) if p.contact else None,
                "oref": p.organization_ref,
                "cb": p.created_by,
                "ub": p.updated_by,
            },
        )


# ---------------------------------------------------------------------------
# Invoice / InvoiceLine / Installment
# ---------------------------------------------------------------------------


class InvoiceRepository:
    def __init__(self, session: AsyncSession, tenant_id: UUID) -> None:
        self.session = session
        self.tenant_id = tenant_id

    async def create(self, inv: Invoice) -> None:
        await self.session.execute(
            text(
                """
                INSERT INTO invoice
                  (id, tenant_id, unit_id, participant_id, payer_party_id,
                   invoice_no, currency, document_status, issue_date, total_amount,
                   policy_id, created_at, created_by, updated_at, updated_by)
                VALUES
                  (:id, :tid, :uid, :pid, :ppid, :ino, :cur, :status, :idate,
                   :total, :pol,
                   now(), :cb, now(), :ub)
                """
            ),
            {
                "id": str(inv.id),
                "tid": str(self.tenant_id),
                "uid": str(inv.unit_id),
                "pid": str(inv.participant_id),
                "ppid": str(inv.payer_party_id) if inv.payer_party_id else None,
                "ino": inv.invoice_no,
                "cur": inv.currency,
                "status": inv.document_status.value,
                "idate": inv.issue_date,
                "total": inv.total_amount,
                "pol": str(inv.policy_id) if inv.policy_id else None,
                "cb": inv.created_by,
                "ub": inv.updated_by,
            },
        )

    async def get(self, id: UUID) -> Invoice | None:
        row = (
            await self.session.execute(
                text(
                    """
                    SELECT id, tenant_id, unit_id, participant_id, payer_party_id,
                           invoice_no, currency, document_status, issue_date,
                           total_amount, policy_id,
                           created_at, created_by, updated_at, updated_by
                    FROM invoice WHERE id = :id AND tenant_id = :tid
                    """
                ),
                {"id": str(id), "tid": str(self.tenant_id)},
            )
        ).first()
        return _to_invoice(row) if row else None

    async def lock_for_update(self, id: UUID) -> Invoice | None:
        row = (
            await self.session.execute(
                text(
                    """
                    SELECT id, tenant_id, unit_id, participant_id, payer_party_id,
                           invoice_no, currency, document_status, issue_date,
                           total_amount, policy_id,
                           created_at, created_by, updated_at, updated_by
                    FROM invoice WHERE id = :id AND tenant_id = :tid
                    FOR UPDATE
                    """
                ),
                {"id": str(id), "tid": str(self.tenant_id)},
            )
        ).first()
        return _to_invoice(row) if row else None

    async def set_status(
        self,
        id: UUID,
        *,
        status: InvoiceDocumentStatus,
        invoice_no: str | None = None,
        issue_date: Any | None = None,
        actor: str,
    ) -> None:
        await self.session.execute(
            text(
                """
                UPDATE invoice
                SET document_status = :s,
                    invoice_no = COALESCE(:ino, invoice_no),
                    issue_date = COALESCE(:idate, issue_date),
                    updated_at = now(),
                    updated_by = :u
                WHERE id = :id AND tenant_id = :tid
                """
            ),
            {
                "id": str(id),
                "tid": str(self.tenant_id),
                "s": status.value,
                "ino": invoice_no,
                "idate": issue_date,
                "u": actor,
            },
        )

    async def next_invoice_no(self) -> str:
        """Per-namespace sequential invoice number."""
        row = (
            await self.session.execute(
                text(
                    """
                    SELECT COALESCE(MAX(CAST(SUBSTRING(invoice_no FROM '([0-9]+)$') AS integer)), 0) AS n
                    FROM invoice
                    WHERE tenant_id = :tid
                      AND invoice_no IS NOT NULL
                      AND invoice_no ~ '[0-9]+$'
                    """
                ),
                {"tid": str(self.tenant_id)},
            )
        ).first()
        n = (row.n or 0) + 1
        return f"INV-{n:06d}"

    async def sum_active_allocations(self, invoice_id: UUID) -> int:
        row = (
            await self.session.execute(
                text(
                    """
                    SELECT COALESCE(SUM(pa.amount), 0)::bigint AS total
                    FROM payment_allocation pa
                    JOIN installment i ON i.id = pa.installment_id
                    WHERE i.invoice_id = :iid AND pa.tenant_id = :tid
                      AND pa.reversed_at IS NULL
                    """
                ),
                {"iid": str(invoice_id), "tid": str(self.tenant_id)},
            )
        ).first()
        return int(row.total or 0)


class InstallmentRepository:
    def __init__(self, session: AsyncSession, tenant_id: UUID) -> None:
        self.session = session
        self.tenant_id = tenant_id

    async def create(self, inst: Installment) -> None:
        await self.session.execute(
            text(
                """
                INSERT INTO installment
                  (id, tenant_id, invoice_id, sequence_no, due_date, due_amount,
                   timezone, created_at, created_by)
                VALUES
                  (:id, :tid, :iid, :sn, :dd, :amt, :tz, now(), :cb)
                """
            ),
            {
                "id": str(inst.id),
                "tid": str(self.tenant_id),
                "iid": str(inst.invoice_id),
                "sn": inst.sequence_no,
                "dd": inst.due_date,
                "amt": inst.due_amount,
                "tz": inst.timezone,
                "cb": inst.created_by,
            },
        )

    async def list_for_invoice(self, invoice_id: UUID) -> list[Installment]:
        rows = (
            await self.session.execute(
                text(
                    """
                    SELECT id, tenant_id, invoice_id, sequence_no, due_date,
                           due_amount, timezone, created_at, created_by
                    FROM installment
                    WHERE invoice_id = :iid AND tenant_id = :tid
                    ORDER BY sequence_no
                    """
                ),
                {"iid": str(invoice_id), "tid": str(self.tenant_id)},
            )
        ).all()
        return [_to_installment(r) for r in rows]

    async def get(self, id: UUID) -> Installment | None:
        row = (
            await self.session.execute(
                text(
                    """
                    SELECT id, tenant_id, invoice_id, sequence_no, due_date,
                           due_amount, timezone, created_at, created_by
                    FROM installment WHERE id = :id AND tenant_id = :tid
                    """
                ),
                {"id": str(id), "tid": str(self.tenant_id)},
            )
        ).first()
        return _to_installment(row) if row else None

    async def lock_for_update(self, id: UUID) -> Installment | None:
        row = (
            await self.session.execute(
                text(
                    """
                    SELECT id, tenant_id, invoice_id, sequence_no, due_date,
                           due_amount, timezone, created_at, created_by
                    FROM installment WHERE id = :id AND tenant_id = :tid
                    FOR UPDATE
                    """
                ),
                {"id": str(id), "tid": str(self.tenant_id)},
            )
        ).first()
        return _to_installment(row) if row else None


# ---------------------------------------------------------------------------
# PaymentSubmission / VerifiedPayment
# ---------------------------------------------------------------------------


class PaymentSubmissionRepository:
    def __init__(self, session: AsyncSession, tenant_id: UUID) -> None:
        self.session = session
        self.tenant_id = tenant_id

    async def create(self, s: PaymentSubmission) -> None:
        await self.session.execute(
            text(
                """
                INSERT INTO payment_submission
                  (id, tenant_id, unit_id, participant_id, invoice_id,
                   submitted_by, claimed_amount, currency, transfer_at,
                   proof_attachment_id, transfer_reference,
                   status, review_decision, created_at, updated_at)
                VALUES
                  (:id, :tid, :uid, :pid, :iid, :sb, :amt, :cur, :ta,
                   :patt, :tref, :status, CAST(:dec AS JSONB),
                   now(), now())
                """
            ),
            {
                "id": str(s.id),
                "tid": str(self.tenant_id),
                "uid": str(s.unit_id),
                "pid": str(s.participant_id),
                "iid": str(s.invoice_id) if s.invoice_id else None,
                "sb": s.submitted_by,
                "amt": s.claimed_amount,
                "cur": s.currency,
                "ta": s.transfer_at,
                "patt": str(s.proof_attachment_id) if s.proof_attachment_id else None,
                "tref": s.transfer_reference,
                "status": s.status.value,
                "dec": _json(s.review_decision) if s.review_decision else None,
            },
        )

    async def get(self, id: UUID) -> PaymentSubmission | None:
        row = (
            await self.session.execute(
                text(
                    """
                    SELECT id, tenant_id, unit_id, participant_id, invoice_id,
                           submitted_by, claimed_amount, currency, transfer_at,
                           proof_attachment_id, transfer_reference,
                           status, review_decision, created_at, updated_at
                    FROM payment_submission WHERE id = :id AND tenant_id = :tid
                    """
                ),
                {"id": str(id), "tid": str(self.tenant_id)},
            )
        ).first()
        return _to_submission(row) if row else None

    async def update_status(
        self,
        id: UUID,
        *,
        status: PaymentSubmissionStatus,
        decision: dict[str, Any] | None,
        actor: str,
    ) -> None:
        await self.session.execute(
            text(
                """
                UPDATE payment_submission
                SET status = :s,
                    review_decision = CAST(:d AS JSONB),
                    updated_at = now()
                WHERE id = :id AND tenant_id = :tid
                """
            ),
            {"id": str(id), "tid": str(self.tenant_id), "s": status.value, "d": _json(decision) if decision else None},
        )


class VerifiedPaymentRepository:
    def __init__(self, session: AsyncSession, tenant_id: UUID) -> None:
        self.session = session
        self.tenant_id = tenant_id

    async def create(self, v: VerifiedPayment) -> None:
        await self.session.execute(
            text(
                """
                INSERT INTO verified_payment
                  (id, tenant_id, unit_id, submission_id, participant_id,
                   recognized_amount, currency, verification, received_date,
                   reversal_total, created_at, created_by)
                VALUES
                  (:id, :tid, :uid, :sid, :pid, :amt, :cur,
                   CAST(:ver AS JSONB), :rd, 0,
                   now(), :cb)
                """
            ),
            {
                "id": str(v.id),
                "tid": str(self.tenant_id),
                "uid": str(v.unit_id),
                "sid": str(v.submission_id),
                "pid": str(v.participant_id),
                "amt": v.recognized_amount,
                "cur": v.currency,
                "ver": _json(v.verification),
                "rd": v.received_date,
                "cb": v.created_by,
            },
        )

    async def get(self, id: UUID) -> VerifiedPayment | None:
        row = (
            await self.session.execute(
                text(
                    """
                    SELECT id, tenant_id, unit_id, submission_id, participant_id,
                           recognized_amount, currency, verification, received_date,
                           reversal_total, created_at, created_by
                    FROM verified_payment WHERE id = :id AND tenant_id = :tid
                    """
                ),
                {"id": str(id), "tid": str(self.tenant_id)},
            )
        ).first()
        return _to_verified(row) if row else None

    async def lock_for_update(self, id: UUID) -> VerifiedPayment | None:
        row = (
            await self.session.execute(
                text(
                    """
                    SELECT id, tenant_id, unit_id, submission_id, participant_id,
                           recognized_amount, currency, verification, received_date,
                           reversal_total, created_at, created_by
                    FROM verified_payment WHERE id = :id AND tenant_id = :tid
                    FOR UPDATE
                    """
                ),
                {"id": str(id), "tid": str(self.tenant_id)},
            )
        ).first()
        return _to_verified(row) if row else None

    async def sum_active_allocations(self, payment_id: UUID) -> int:
        row = (
            await self.session.execute(
                text(
                    """
                    SELECT COALESCE(SUM(amount), 0)::bigint AS total
                    FROM payment_allocation
                    WHERE payment_id = :pid AND tenant_id = :tid
                      AND reversed_at IS NULL
                    """
                ),
                {"pid": str(payment_id), "tid": str(self.tenant_id)},
            )
        ).first()
        return int(row.total or 0)

    async def bump_reversal_total(self, id: UUID, amount: int) -> None:
        await self.session.execute(
            text(
                "UPDATE verified_payment SET reversal_total = reversal_total + :amt "
                "WHERE id = :id AND tenant_id = :tid"
            ),
            {"id": str(id), "tid": str(self.tenant_id), "amt": amount},
        )


# ---------------------------------------------------------------------------
# PaymentAllocation
# ---------------------------------------------------------------------------


class PaymentAllocationRepository:
    def __init__(self, session: AsyncSession, tenant_id: UUID) -> None:
        self.session = session
        self.tenant_id = tenant_id

    async def create(self, a: PaymentAllocation) -> None:
        await self.session.execute(
            text(
                """
                INSERT INTO payment_allocation
                  (id, tenant_id, payment_id, installment_id, amount,
                   allocated_at, allocated_by, idempotency_id)
                VALUES
                  (:id, :tid, :pid, :iid, :amt, :at, :ab, :idem)
                """
            ),
            {
                "id": str(a.id),
                "tid": str(self.tenant_id),
                "pid": str(a.payment_id),
                "iid": str(a.installment_id),
                "amt": a.amount,
                "at": a.allocated_at,
                "ab": a.allocated_by,
                "idem": str(a.idempotency_id) if a.idempotency_id else None,
            },
        )

    async def get(self, id: UUID) -> PaymentAllocation | None:
        row = (
            await self.session.execute(
                text(
                    """
                    SELECT id, tenant_id, payment_id, installment_id, amount,
                           allocated_at, allocated_by, idempotency_id,
                           reversed_at, reversal_reason
                    FROM payment_allocation WHERE id = :id AND tenant_id = :tid
                    """
                ),
                {"id": str(id), "tid": str(self.tenant_id)},
            )
        ).first()
        return _to_allocation(row) if row else None

    async def reverse(self, id: UUID, *, reason: str, actor: str) -> None:
        await self.session.execute(
            text(
                """
                UPDATE payment_allocation
                SET reversed_at = now(),
                    reversal_reason = :r,
                    updated_at = now()
                WHERE id = :id AND tenant_id = :tid AND reversed_at IS NULL
                """
            ),
            {"id": str(id), "tid": str(self.tenant_id), "r": reason},
        )


# ---------------------------------------------------------------------------
# Receipt
# ---------------------------------------------------------------------------


class ReceiptRepository:
    def __init__(self, session: AsyncSession, tenant_id: UUID) -> None:
        self.session = session
        self.tenant_id = tenant_id

    async def create(self, r: Receipt) -> None:
        await self.session.execute(
            text(
                """
                INSERT INTO receipt
                  (id, tenant_id, payment_id, receipt_no, snapshot, status,
                   supersedes_id, created_at, created_by)
                VALUES
                  (:id, :tid, :pid, :rno, CAST(:snap AS JSONB), :status,
                   :sup, now(), :cb)
                """
            ),
            {
                "id": str(r.id),
                "tid": str(self.tenant_id),
                "pid": str(r.payment_id),
                "rno": r.receipt_no,
                "snap": _json(r.snapshot),
                "status": r.status.value,
                "sup": str(r.supersedes_id) if r.supersedes_id else None,
                "cb": r.created_by,
            },
        )

    async def get(self, id: UUID) -> Receipt | None:
        row = (
            await self.session.execute(
                text(
                    """
                    SELECT id, tenant_id, payment_id, receipt_no, snapshot, status,
                           supersedes_id, created_at, created_by
                    FROM receipt WHERE id = :id AND tenant_id = :tid
                    """
                ),
                {"id": str(id), "tid": str(self.tenant_id)},
            )
        ).first()
        return _to_receipt(row) if row else None

    async def next_receipt_no(self) -> str:
        row = (
            await self.session.execute(
                text(
                    """
                    SELECT COALESCE(MAX(CAST(SUBSTRING(receipt_no FROM '([0-9]+)$') AS integer)), 0) AS n
                    FROM receipt
                    WHERE tenant_id = :tid
                      AND receipt_no ~ '[0-9]+$'
                    """
                ),
                {"tid": str(self.tenant_id)},
            )
        ).first()
        n = (row.n or 0) + 1
        return f"RCP-{n:06d}"


# ---------------------------------------------------------------------------
# FinancialAdjustment
# ---------------------------------------------------------------------------


class FinancialAdjustmentRepository:
    def __init__(self, session: AsyncSession, tenant_id: UUID) -> None:
        self.session = session
        self.tenant_id = tenant_id

    async def create(self, fa: FinancialAdjustment) -> None:
        await self.session.execute(
            text(
                """
                INSERT INTO financial_adjustment
                  (id, tenant_id, adjustment_kind, source_kind, source_id,
                   installment_id, amount, decision, effective_at,
                   idempotency_id, policy_id, created_at, created_by)
                VALUES
                  (:id, :tid, :ak, :sk, :sid, :iid, :amt,
                   CAST(:dec AS JSONB), :ea, :idem, :pol, now(), :cb)
                """
            ),
            {
                "id": str(fa.id),
                "tid": str(self.tenant_id),
                "ak": fa.adjustment_kind.value,
                "sk": fa.source_ref.get("kind", ""),
                "sid": str(UUID(fa.source_ref["id"])) if fa.source_ref.get("id") else None,
                "iid": str(fa.installment_id) if fa.installment_id else None,
                "amt": fa.amount,
                "dec": _json(fa.decision),
                "ea": fa.effective_at,
                "idem": str(fa.idempotency_id) if fa.idempotency_id else None,
                "pol": str(fa.policy_id) if fa.policy_id else None,
                "cb": fa.created_by,
            },
        )


# ---------------------------------------------------------------------------
# Mapping
# ---------------------------------------------------------------------------


def _to_invoice(row: Any) -> Invoice:
    return Invoice(
        id=row.id,
        tenant_id=row.tenant_id,
        unit_id=row.unit_id,
        participant_id=row.participant_id,
        payer_party_id=row.payer_party_id,
        invoice_no=row.invoice_no,
        currency=row.currency,
        document_status=InvoiceDocumentStatus(row.document_status),
        issue_date=row.issue_date,
        total_amount=int(row.total_amount),
        policy_id=row.policy_id,
        created_at=row.created_at,
        created_by=row.created_by,
        updated_at=row.updated_at,
        updated_by=row.updated_by,
    )


def _to_installment(row: Any) -> Installment:
    return Installment(
        id=row.id,
        tenant_id=row.tenant_id,
        invoice_id=row.invoice_id,
        sequence_no=row.sequence_no,
        due_date=row.due_date,
        due_amount=int(row.due_amount),
        timezone=row.timezone,
        created_at=row.created_at,
        created_by=row.created_by,
    )


def _to_submission(row: Any) -> PaymentSubmission:
    return PaymentSubmission(
        id=row.id,
        tenant_id=row.tenant_id,
        unit_id=row.unit_id,
        participant_id=row.participant_id,
        invoice_id=row.invoice_id,
        submitted_by=row.submitted_by,
        claimed_amount=int(row.claimed_amount),
        currency=row.currency,
        transfer_at=row.transfer_at,
        proof_attachment_id=row.proof_attachment_id,
        transfer_reference=row.transfer_reference,
        status=PaymentSubmissionStatus(row.status),
        review_decision=row.review_decision,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def _to_verified(row: Any) -> VerifiedPayment:
    return VerifiedPayment(
        id=row.id,
        tenant_id=row.tenant_id,
        unit_id=row.unit_id,
        submission_id=row.submission_id,
        participant_id=row.participant_id,
        recognized_amount=int(row.recognized_amount),
        currency=row.currency,
        verification=row.verification,
        received_date=row.received_date,
        reversal_total=int(row.reversal_total),
        created_at=row.created_at,
        created_by=row.created_by,
    )


def _to_allocation(row: Any) -> PaymentAllocation:
    return PaymentAllocation(
        id=row.id,
        tenant_id=row.tenant_id,
        payment_id=row.payment_id,
        installment_id=row.installment_id,
        amount=int(row.amount),
        allocated_at=row.allocated_at,
        allocated_by=row.allocated_by,
        idempotency_id=row.idempotency_id,
    )


def _to_receipt(row: Any) -> Receipt:
    return Receipt(
        id=row.id,
        tenant_id=row.tenant_id,
        payment_id=row.payment_id,
        receipt_no=row.receipt_no,
        snapshot=row.snapshot,
        status=ReceiptStatus(row.status),
        supersedes_id=row.supersedes_id,
        created_at=row.created_at,
        created_by=row.created_by,
    )