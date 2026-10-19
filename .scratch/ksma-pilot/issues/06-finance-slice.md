# 06: Finance slice — invoice, payment, allocation, reversal end-to-end

**What to build:** A complete money flow: FI issues an invoice with two installments totaling 1,000,000; PY submits payment evidence; FI verifies with a possibly-different recognized amount; FI allocates verified funds to installments under a `FOR UPDATE` transaction; receipt is issued; reversals are audited and atomic. After this ticket, the worked example from the PRD (V13–V18) is demoable end-to-end.

**Blocked by:** 00, 01, 02, 03 (participants + enrollments must exist)

**Status:** ready-for-agent

- [ ] `POST /api/v1/invoices` creates a draft `Invoice` with `currency='IDR'`, `policy_id`, optional `payer_party_id`; `document_status='draft'`; line totals and installment sum must equal `total_amount` (R12, V13); rejects negative `net_amount`.
- [ ] `POST /api/v1/invoices/{id}/lines` adds `InvoiceLine` rows; recomputes total and rejects if any line violates the invariant.
- [ ] `POST /api/v1/invoices/{id}/installments` adds `Installment` rows with `sequence_no`, `due_date`, `due_amount`, `timezone`; sum must equal `total_amount` (R12, V13); rejects if any installment is past due_date for an invoice still in `draft` (Q04 deferred otherwise).
- [ ] `POST /api/v1/invoices/{id}/issue` allocates `invoice_no`, sets `issue_date`, freezes totals; rejects if no lines or no installments.
- [ ] `POST /api/v1/invoices/{id}/void` requires a reason; refuses if there are effective allocations (those must be reversed first).
- [ ] `POST /api/v1/payments` (FIN-A03) submits a `PaymentSubmission` with `participant_id`, optional `invoice_id`, `claimed_amount`, `currency`, optional `transfer_at`, optional `proof_attachment_id`, optional `transfer_reference`; status `submitted`; suspected-duplicate flag returned (same ref + amount + date in last 30 days).
- [ ] `POST /api/v1/payments/{id}/verify` (FIN-A04) transitions to `verified` with `recognized_amount` and a `verification` decision (actor + reason); writes a `VerifiedPayment` row with `received_date`; `claimed ≠ recognized` is allowed with a `difference_reason` (V14).
- [ ] `POST /api/v1/payments/{id}/reject` transitions to `rejected` with a `review_decision` reason; the original submission is preserved.
- [ ] `POST /api/v1/allocations` (FIN-A05) creates a `PaymentAllocation` inside one transaction:
  1. `BEGIN; SELECT … FOR UPDATE on verified_payment;`
  2. `SELECT … FOR UPDATE on installment;`
  3. Recompute `available = recognized - reversals - effective_allocations` and `remainder = due_amount - credits - effective_allocations`.
  4. Reject 409 `INSUFFICIENT_AVAILABLE_FUNDS` if `amount > available` (V15), or `EXCEEDS_INSTALLMENT_REMAINDER` if `amount > remainder`.
  5. INSERT allocation; COMMIT.
  Requires `Idempotency-Key` header; same key + same body → cached response; same key + different body → 409 `IDEMPOTENCY_CONFLICT` (V16); replay after grant revocation → 403.
- [ ] `POST /api/v1/allocations/{id}/reverse` creates `FinancialAdjustment(adjustment_kind='allocation_reversal', source_ref={kind:'payment_allocation', id})` with `decision.reason`; refuses if it would drive available funds negative (V18).
- [ ] `POST /api/v1/payments/{id}/reverse` creates `FinancialAdjustment(adjustment_kind='payment_reversal', source_ref={kind:'verified_payment', id})` and reverses related allocations in the same transaction (R15).
- [ ] `POST /api/v1/invoices/{id}/credits` creates `FinancialAdjustment(adjustment_kind='invoice_credit', installment_id, amount, reason)`; refuses if amount > installment remainder (R15, V17).
- [ ] `GET /api/v1/invoices/{id}` returns the canonical read view plus projections: `paid_status ∈ {unpaid, partial, paid, settled_zero}`, `arrears = sum(remainder where due_date <= today)`, never equal to `total_amount - total_remainder` (BR08).
- [ ] `GET /api/v1/participants/{id}/arrears?at=YYYY-MM-DD` returns the arrears projection for a date.
- [ ] `GET /api/v1/receipts/{id}` returns the receipt including `attachment_id` (downloadable via `GET /api/v1/attachments/{id}/content` after rights check).
- [ ] Integration tests for: V13 line/instalment sum rejection, V14 claimed ≠ recognized, V15 insufficient funds (sequential and concurrent), V16 idempotent allocation replay, V17 credit exceeding remainder, V18 payment reversal without reversing allocations rejected.