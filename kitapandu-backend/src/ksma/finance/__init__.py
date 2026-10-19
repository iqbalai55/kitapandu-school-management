"""KSMA Finance bounded context.

Owns:
- BillingParty
- Invoice, InvoiceLine, Installment
- PaymentSubmission, VerifiedPayment, PaymentAllocation
- Receipt, FinancialAdjustment

Invariants (BR06, R12–R15, R21):
- Money stored as integer in smallest unit + Currency; IDR scale 0; never negative
- Invoice ``document_status`` is stored; ``paid/partial/unpaid/overdue`` are projections
- PaymentAllocation is allowed (R14) only if ``amount ≤ available funds`` AND
  ``amount ≤ installment_remainder`` — both checked atomically with FOR UPDATE
- PaymentVerification commits submission→verified + exactly one VerifiedPayment in
  one transaction (R13)
- Reversal of allocated funds reverses related allocations consistently (R15)
- Allocation endpoints require ``Idempotency-Key`` header (R21)
- Q04 finance policy must be ``published`` before any invoice/payment flow runs
"""