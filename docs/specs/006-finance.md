# Spec 006 — Finance Bounded Context

> `BillingParty`, `Invoice`, `InvoiceLine`, `Installment`, `PaymentSubmission`,
> `VerifiedPayment`, `PaymentAllocation`, `Receipt`, `FinancialAdjustment`.
> PRD module codes FIN01–04, BR06, BR07, BR08, BR09, R12, R13, R14, R15,
> V13, V14, V15, V16, V17, V18.

## Problem Statement

Money. The most dangerous surface in the system. Every cent is a cent;
allocations race under concurrent requests; reversing a payment must not
underflow remaining funds; arrears is a derived value from installments
that have actually fallen due, not the total invoice balance. Money math
never erases history.

## Solution

A single `finance` bounded context that owns the entire money lifecycle
from invoice issuance through payment verification, allocation, receipt
issuance, and authorised reversal. Money is stored as `NUMERIC(18, 0)` (integer
minor units), computed via `Money` value object, and protected by
`SELECT … FOR UPDATE` invariants in transactional allocations.

## User Stories

### Invoice
1. As **FI**, I want to create a `Invoice` for a participant with `currency`,
   `total_amount`, `policy_id`, optional `payer_party_id`, so that the obligation
   is recorded.
2. As **FI**, I want to add `InvoiceLine` rows with `description`, optional
   `enrollment_id`, `quantity`, `unit_price`, `discount_amount`, `net_amount`,
   so that the obligation is itemised.
3. As **FI**, I want `net_amount` to be non-negative and the sum of lines to
   equal `total_amount` (R12, V13).
4. As **FI**, I want to add `Installment` rows (`sequence_no`, `due_date`,
   `due_amount`, `timezone`) whose sum equals `total_amount` (R12, V13).
5. As **FI**, I want the invoice to remain in `status='draft'` until I issue it;
   once issued (`status='issued'`, `invoice_no` allocated, `issue_date` set),
   the totals are frozen.
6. As **FI**, I want the invoice number (`invoice_no`) to be unique per tenant.

### Status projection
7. As any **user**, I want the invoice's "paid / overdue" state to be a
   *projection* of allocations + due dates, NOT a `document_status` field
   (PRD §4.7). The schema doesn't store "paid" — it's computed.
8. As **FI**, I want `arrears` (tunggakan) to be the sum of installment remainders
   that have `due_date <= today`, NOT the total invoice balance (R12, BR08).
9. As **FI**, I want `arrears` to NOT auto-stop learning or change grades
   (BR08) — that's a separate policy decision (Q04).

### Payment submission and verification
10. As **ST/PY/FI**, I want to submit a `PaymentSubmission` with `claimed_amount`,
    `currency`, `transfer_at?`, optional `proof_attachment_id`, optional
    `transfer_reference`, so that the review can begin.
11. As **FI**, I want to verify a submission (`status='verified'`) with
    `recognized_amount` and a `verification` decision (actor + reason + time);
    the `VerifiedPayment` row is the receipt.
12. As **FI**, I want the recognized amount to differ from claimed (V14) with
    a `reason` recorded, so that mismatches are auditable.
13. As **FI**, I want to reject a submission (`status='rejected'`) with a reason;
    the prior submission is preserved.
14. As **FI**, I want suspected duplicate submissions (same reference + amount +
    date) to be flagged for review (BR06), not auto-merged.

### Allocation
15. As **FI**, I want to allocate verified funds to an installment via
    `PaymentAllocation` with `amount` ≤ available funds and ≤ installment remainder
    (R14, V15).
16. As **FI**, I want the allocation to be **idempotent** — replaying the same
    `Idempotency-Key` returns the cached result, NOT a second allocation (V16).
17. As **FI**, I want two concurrent allocations on the last funds to be safe:
    one succeeds, the other gets 409 `INSUFFICIENT_AVAILABLE_FUNDS` (V15).
18. As **FI**, I want the allocation's tenant, unit, participant, currency to
    match between payment and installment (R14), so that cross-tenant/cross-currency
    allocations are impossible.

### Receipt
19. As **FI**, I want the receipt (`Receipt`) to be a snapshot at issuance,
    with `payment_id`, `allocation_ids[]`, `receipt_no` (unique per tenant),
    `snapshot`, optional `attachment_id`, `status='issued'`.
20. As **FI**, I want receipt replacements (`status='replaced'`) to keep the
    original on the chain via `supersedes_id`.

### Adjustment
21. As **FI**, I want to issue a `FinancialAdjustment(adjustment_kind='invoice_credit')`
    for a positive credit to an installment; the credit must not exceed the
    installment remainder (R15, V17).
22. As **FI**, I want to issue `allocation_reversal` for an existing allocation;
    the reversal must not drive available funds negative and must release the
    related allocations consistently (V18).
23. As **FI**, I want to issue `payment_reversal` for a verified payment;
    related allocations must be reversed in the same transaction (R15).
24. As **FI**, I want all reversals to require `decision.decision.decided_at`,
    actor, reason (BR07, R15) and `idempotency_id` (R21).
25. As **FI**, I want a bank refund to be a separate flow (Q04 deferred); the
    `payment_reversal` does NOT automatically issue a bank refund.

## Implementation Decisions

### Schema (already migrated, see `alembic/versions/0007_finance.py`)
- `billing_party(id, tenant_id, unit_id?, party_type, display_name, contact?, …)`.
- `invoice(id, tenant_id, unit_id, participant_id, invoice_no?, currency, document_status
  ('draft'|'issued'|'void'), issue_date?, total_amount NUMERIC(18,0), policy_id,
  payer_party_id?, …)`. Unique `(tenant_id, invoice_no)` (when issued).
- `invoice_line(id, tenant_id, unit_id, invoice_id, line_no, description, enrollment_id?,
  quantity, unit_price NUMERIC(18,0), discount_amount NUMERIC(18,0), net_amount
  NUMERIC(18,0), …)`. Unique `(invoice_id, line_no)`. CHECK `net_amount >= 0`.
- `installment(id, tenant_id, unit_id, invoice_id, sequence_no, due_date, due_amount
  NUMERIC(18,0), timezone, …)`. Unique `(invoice_id, sequence_no)`. CHECK `due_amount > 0`.
- `payment_submission(id, tenant_id, unit_id, participant_id, invoice_id?, submitted_by,
  claimed_amount, currency, transfer_at?, proof_attachment_id?, transfer_reference?, status
  ('submitted'|'under_review'|'verified'|'rejected'), review_decision?, …)`.
- `verified_payment(id, tenant_id, unit_id, submission_id, participant_id, recognized_amount
  NUMERIC(18,0), currency, verification JSONB, received_date, reversal_total NUMERIC(18,0)
  DEFAULT 0, …)`.
- `payment_allocation(id, tenant_id, unit_id, payment_id, installment_id, amount
  NUMERIC(18,0), allocated_at, allocated_by, idempotency_id, …)`. CHECK `amount > 0`.
- `receipt(id, tenant_id, unit_id, payment_id, allocation_ids[], receipt_no, snapshot JSONB,
  attachment_id?, status ('issued'|'replaced'|'void'), supersedes_id?, …)`.
  Unique `(tenant_id, receipt_no)`.
- `financial_adjustment(id, tenant_id, unit_id, adjustment_kind ('invoice_credit'|
  'allocation_reversal'|'payment_reversal'), source_ref JSONB, installment_id?,
  amount NUMERIC(18,0), decision JSONB, effective_at, idempotency_id, policy_id, …)`.

### Money invariants (R12, R13, R14, R15)
- All currency fields are `NUMERIC(18, 0)` with `CHECK >= 0` where applicable.
- Allocation runs inside one transaction:
  1. `BEGIN; SELECT … FOR UPDATE on verified_payment WHERE id=:pid`.
  2. `SELECT … FOR UPDATE on installment WHERE id=:iid`.
  3. Recompute `available = recognized_amount - payment_reversal_total
     - sum(effective_allocations)` and `remainder = due_amount - credits
     - sum(effective_allocations)`.
  4. If `amount > available OR amount > remainder` → 409 with code
     `INSUFFICIENT_AVAILABLE_FUNDS` or `EXCEEDS_INSTALLMENT_REMAINDER`.
  5. INSERT allocation, increment allocation totals.
  6. COMMIT.
- `verified_payment.reversal_total` and `payment_allocation` total are kept in sync
  via `FinancialAdjustment` writes; never mutated directly.
- The `Idempotency-Key` header is enforced on `POST /allocations`, `POST /payments`,
  `POST /financial-adjustments` per ADR-0001 R21.

### Arrears computation
- `arrears_for_participant(participant_id, *, at=None)` is a stored function:
  `SELECT SUM(effective_obligation - effective_allocations) FROM installment
   WHERE invoice.participant_id = :pid AND due_date <= :at AND tenant_id = :tid`.
- Never stored on the invoice; never the total invoice balance.

### Currency
- Pilot: IDR only (R12, Q04). `Currency` enum has only `IDR` for the pilot.
- Cross-currency conversion is out of scope (Q04).

### Seams
- **Inbound**: portal reads invoices/receipts for relations; reporting reads nothing
  (BR09 forbids mixing money with academic completion).
- **Outbound**: finance depends on `participant.domain.Participant` (FK only),
  `identity.domain.PolicyVersion` (for `policy_id`).
- **ACL exports**: `finance.acl.invoices_for_participant(participant_id, requester_id,
  scope)` for portal filtering.

## Testing Decisions

What makes a good test:
- Asserts **DB state** plus the projection: after issuing an invoice with two
  installments, two verified payments, two allocations, the API returns
  `paid_status='partial'` and `arrears=1000000` (using V17's worked example).
- V13: line net totals sum check; installment sum check.
- V14: recognized_amount differs from claimed → the difference is recorded with a reason.
- V15: available funds test — funds=1000000, allocations=700000, attempting a new
  allocation of 350000 returns 409.
- V16: idempotency test — two concurrent allocations with the same `Idempotency-Key`
  return identical responses; one succeeds, the other returns the cached response.
- V17: invoice credit exceeding installment remainder returns 409.
- V18: payment reversal without reversing allocations returns 409 (would drive
  available funds negative).
- Allocations are computed exactly once per pass (no double-counting on retries).

## Out of Scope

- Payment gateway integration (P1).
- Bank refund flow (Q04 deferred).
- Cross-currency conversion (Q04).
- Automatic dunning notifications (PRD allows but Q09 pending).
- Outstanding-balance reports cross-invoice (BR08: arrears per installment, not invoice).

## Further Notes

- The existing `src/ksma/finance/` folder (~3,900 lines) is the largest BC and
  covers the full money flow. The hardest invariants (V15, V16, V17, V18) are
  the highest-risk test targets.
- The existing implementation already uses raw-SQL with explicit `FOR UPDATE`
  patterns where needed. This spec validates the existing approach.
- Remaining work:
  1. Confirm allocation transaction explicitly uses `FOR UPDATE` on both payment
     and installment rows (the existing code is structured this way but
     needs review for race correctness).
  2. Add an `arrears_for_participant(at)` stored SQL function + a unit test.
  3. Add integration tests for V13–V18.
  4. Add a `projection` view that joins invoice + allocations + installments for
     the dashboard.