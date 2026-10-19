# 08: Portal slice — service request, inbox, work-item handoff

**What to build:** A complete portal flow: ST/GU/PY submits a `ServiceRequest` with `Idempotency-Key`, AC/FI triages it; the in-app inbox shows notifications triggered by finance / assessment / session / batch events; outbox subscribers automatically create `ServiceRequest(requester_kind='service')` rows for cross-BC handoffs (e.g., enrollment transfer → finance review). After this ticket, R17 (request doesn't mutate source) and V20/V21 (notification dedup) are demoable.

**Blocked by:** 00, 01, 02 (identity), 03 (participant/relation scoping), 04 (program events), 05 (session events), 06 (finance events), 07 (reporting events)

**Status:** ready-for-agent

- [ ] `POST /api/v1/portal/requests` (PRT-A05) submits a `ServiceRequest` with `participant_id`, `category ∈ {academic, schedule, identity, finance}`, `description`, optional `source_ref`; auto-generates `requestNumber`; requires `Idempotency-Key` header; same key + different body → 409 `IDEMPOTENCY_CONFLICT` (V22); emits outbox event.
- [ ] `POST /api/v1/portal/requests/{id}/resolve` (PRT-A06) transitions to `approved | rejected | closed` with a `resolution` JSONB (actor + reason); does NOT auto-execute the source action (R17).
- [ ] `GET /api/v1/portal/inbox?limit=50` returns the recipient's `InternalNotification` rows; re-checks relation on every fetch; doesn't return messages whose relation has been revoked.
- [ ] `POST /api/v1/portal/notifications/{id}/read` marks `read_at`.
- [ ] `POST /api/v1/portal/communication-preferences` upserts a per-account opt-in for `(category, channel ∈ {internal, manual_template})` with optional `contact_reference`.
- [ ] Outbox subscriber for `EnrollmentChanged` writes a `ServiceRequest(requester_kind='service', category='finance', system_event_id=<outbox_event_id>)` for FI to action; the work item's `source_ref` points at the enrollment change.
- [ ] Outbox subscriber for `ReportPublished` writes a `ServiceRequest(requester_kind='service', category='academic', system_event_id=…)` for AP to record a `CompletionDecision`.
- [ ] Outbox subscriber for `ImportBatchNeedsCorrection` writes a `ServiceRequest(requester_kind='service', category='identity', system_event_id=…)` for the data-owning admin.
- [ ] Outbox subscribers for `InvoiceIssued`, `PaymentVerified`, `FundsAllocated`, `SessionScheduled` write `InternalNotification` rows with `ON CONFLICT (tenant_id, recipient_account_id, event_id, notification_kind) DO NOTHING` (R20, V21).
- [ ] `ops_work_queue` SQL view (created in a small migration) joins `ServiceRequest(status IN ('open','in_review'))` UNION ALL `InternalNotification(delivery_state='available')` with column `age_since_submission` for the dashboard sort.
- [ ] `GET /api/v1/portal/requests` filters by `category`, `status`, `assigned_membership_id`; returns only items the requester is allowed to see (AC/FI see all in their tenant; ST/GU/PY see only items related to their linked participants).
- [ ] Integration tests for: V21 dedup (same event twice → one notification + one ProcessedEvent), V22 idempotency-key conflict, R17 service request resolution doesn't mutate source, GU relation revoke → next inbox read filters out messages from the revoked relation.