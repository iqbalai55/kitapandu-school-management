# Spec 007 — Portal Bounded Context

> `ServiceRequest`, `InternalNotification`, `CommunicationPreference`.
> PRD module codes PRT01–04, R17, V20, V21, V22.

## Problem Statement

Three different audiences — staff, participants, and guardians — need a single
place to talk to each other. PRT01 service wants a portal that shows effective
schedule, attendance, shared material, published reports, and finance info per
relation. PRT02 wants verified guardian/payer relations with scoped access.
PRT03 wants internal notifications and a service request system for leave,
billing questions, and change requests. PRT04 wants communication preferences
and template support (manual delivery in pilot).

## Solution

A `portal` bounded context that owns:
- The `ServiceRequest` table (also reused as `WorkItem` per ADR-0003 for
  system-generated handoffs).
- The `InternalNotification` table (in-app inbox).
- The `CommunicationPreference` table (per-account opt-in for category + channel).

The pilot uses internal and `manual_template` channels only (Q09 defers
WhatsApp/email/SMS). The cross-BC `ServiceRequest` reuse is documented in
ADR-0003: a `system_generated: true` flag plus a `system_event_id` column link
the work item to the originating `OutboxEvent`.

## User Stories

### Portal access
1. As **ST**, I want to open the academic portal and see only MY effective
   schedule, attendance, shared material, and published reports (PRT01).
2. As **GU**, I want to open the portal and see only the participants I'm an
   active `academic_guardian` of; I do NOT see participants I'm only a `payer` for.
3. As **PY**, I want to open the financial portal and see only the invoices
   within my `financial_scope`; I do NOT see attendance or grades.

### Service request
4. As **ST/GU/PY/IN**, I want to submit a `ServiceRequest` with
   `category ∈ {'academic', 'schedule', 'identity', 'finance'}`, `description`,
   optional `source_ref`, and an auto-generated request number.
5. As **ST/GU/PY**, I want my request to NOT directly change schedules or money
   (R17); it's a ticket, not an action.
6. As **AC/FI**, I want my request list filtered by `category` and `status`
   so that I see only what's relevant.
7. As **AC/FI**, I want to take ownership of a request (`assigned_membership_id`),
   mark it `in_review`, then `approved` or `rejected` with `resolution`.
8. As **AC/FI**, I want my resolution to record actor + reason, so that
   decisions are traceable (BR07).
9. As **AC/FI**, I want my approved resolution to NOT auto-execute the source
   action — the resolver needs separate authority to do that (R17).

### Internal notification
10. As any **user**, I want my in-app inbox to show notifications triggered by
    outbox events: `InvoiceIssued`, `PaymentVerified`, `FundsAllocated`,
    `SessionScheduled`, `ReportPublished` (per existing main.py stub).
11. As any **user**, I want one notification per `(event, recipient, kind)` —
    no duplicates (R20, V21).
12. As any **user**, I want my notification's `summary` to be safe for shared
    screens — no payment details, no grades, no PII beyond what's needed.
13. As any **user**, I want my notification's relation to be checked at creation
    AND at fetch time, so that revoked relations don't keep showing the message.
14. As any **user**, I want a "manual delivery" status to be distinct from a
    provider's "delivered" status (BR11); pilot uses manual copies.

### Communication preference
15. As **ST/GU/PY**, I want to set my preference for a `(category, channel)`
    pair, with a `contact_reference` for the manual-template prep.
16. As **ST/GU/PY**, I want my preference to NOT auto-create a relation or
    open a channel — it's just a flag.
17. As any **user**, I want my preference uniqueness to be
    `(account, optional participant, category, channel)` so that I can have
    different preferences per scope.

### Work-item handoff (ADR-0003)
18. As a **finance officer (FI)**, I want a `WorkItem` to appear in my queue
    when an `EnrollmentChange` (transfer, pause, withdraw) commits, so that
    I know to review the invoice impact (UX F01 step 5).
19. As a **data owner**, I want a `WorkItem` to appear when an `ImportBatch`
    completes with `status='needs_correction'`, so that I can fix the source.
20. As an **AP**, I want a `WorkItem` to appear when a `ReportPublication` is
    `published` and `CompletionDecision` is needed.
21. As any **staff**, I want a `WorkItem` to link to the originating
    `OutboxEvent` so that I can audit the chain.

## Implementation Decisions

### Schema (already migrated, see `alembic/versions/0009_portal_operations.py`)
- `service_request(id, tenant_id, unit_id?, requester_account_id, participant_id,
  category ('academic'|'schedule'|'identity'|'finance'), source_ref JSONB?,
  description, assigned_membership_id?, status ('open'|'in_review'|'approved'|'rejected'|'closed'),
  resolution JSONB?, requester_kind ('account'|'service') DEFAULT 'account',
  system_event_id? UUID, …)`.
- `internal_notification(id, tenant_id, recipient_account_id, source_ref JSONB,
  event_id?, notification_kind, summary, delivery_state ('pending'|'available'|'suppressed'),
  read_at?, occurred_at, …)`. Unique `(tenant_id, recipient_account_id, event_id, notification_kind)`.
- `communication_preference(id, tenant_id, account_id, participant_id?, category,
  channel ('internal'|'manual_template'), enabled, contact_reference?, …)`.
  Unique `(tenant_id, account_id, participant_id, category, channel)` with NULL-aware
  semantics for `participant_id`.

### Work-item reuse (ADR-0003)
- `ServiceRequest(requester_kind='service', system_event_id=<outbox_event_id>)`
  for system-generated handoffs.
- Outbox handlers for `EnrollmentChanged`, `ImportBatchNeedsCorrection`,
  `ReportPublished` write a `ServiceRequest` per ADR-0003.
- Dashboard view: `ops_work_queue` SQL view joining `ServiceRequest` (open|in_review)
  and `InternalNotification` (available) per the dashboard spec.

### Idempotency on request creation
- `Idempotency-Key` header enforced on `POST /portal/requests` (R21).
- Same key + same description + same source_ref → cached response.
- Same key + different input → 409 `IDEMPOTENCY_CONFLICT`.

### Notification dedup (R20, V21)
- The `internal_notification` unique constraint on
  `(tenant_id, recipient_account_id, event_id, notification_kind)` prevents
  duplicate writes.
- Outbox handler for `InvoiceIssued` etc. writes `ON CONFLICT DO NOTHING`.

### Seams
- **Inbound**: every BC that emits a notifiable event (finance, assessment,
  session, batch, import) writes the `InternalNotification` row in the
  notification handler that subscribes to the outbox event.
- **Outbound**: portal depends on `identity.domain.Account`,
  `participant.domain.Participant`. Reads other BCs only via ACL ports.
- **ACL exports**: `portal.acl.requests_for_membership(membership_id)`,
  `portal.acl.inbox_for_account(account_id)`.

## Testing Decisions

What makes a good test:
- Asserts that a denied relation read returns 403 with no payload (no name leakage).
- V21: emitting the same outbox event twice produces one notification row.
- V22: same `Idempotency-Key` with different body returns 409.
- Work-item handoff: enrollment transfer emits `EnrollmentChanged` event;
  outbox worker processes it; a `ServiceRequest(requester_kind='service',
  category='finance')` row is created with `system_event_id=<enrollment_change_event_id>`.
- Portal filtering: GU active for participant A and B; portal read returns
  A's schedule but not B's; PY active for A only sees A's invoices.

## Out of Scope

- WhatsApp / SMS / email provider integration (Q09 pending).
- Notification preference-based auto-routing (preferences are stored but not
  consumed by external channels yet).
- Push notifications to mobile (P1+).

## Further Notes

- The existing `src/ksma/portal/` folder (~440 lines) covers `application`
  and `interface` for service requests and the inbox. The `domain/` and
  `infrastructure/` folders don't exist yet but the tables and patterns are
  already in the migrations.
- The `ops_work_queue` view is referenced in spec 009 (operations); portal
  contributes the `ServiceRequest` half.
- Remaining work:
  1. Implement `portal/infrastructure/repositories.py` and `portal/domain/__init__.py`.
  2. Wire the outbox subscribers for `EnrollmentChanged`,
     `ImportBatchNeedsCorrection`, `ReportPublished` to write the corresponding
     `ServiceRequest` rows.
  3. Add `ops_work_queue` SQL view in migration `0009` (or as a new migration).
  4. Add unit tests for `mask_response` and `Idempotency-Key` enforcement.
  5. Add integration tests for V20, V21, V22.