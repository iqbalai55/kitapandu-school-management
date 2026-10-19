# Spec 008 — Operations Bounded Context

> `Attachment`, `ImportBatch`, `ImportRowResult`, `ExportJob`, `MetricDefinition`,
> `OperationalRun`, `BackgroundJob`, `ProcessedEvent`, `IdempotencyRecord`, plus
> shared kernel modules for outbox and idempotency. PRD module codes OPS01–04,
> NFR07–10, R12, R16, R18, R19, R20, R21, R23, R24, V19, V20, V23, V24.

## Problem Statement

Operations are the cross-cutting things that aren't academic, financial, or
identity: file attachments with content checking, bulk import/export with
preview and atomic commit, metric definitions for the dashboard, ops runs for
backup/restore evidence, background jobs for the outbox worker, dedup records
for event consumers, and idempotency records for protected POSTs. These
powers every other BC; failures here are observable everywhere.

## Solution

An `operations` bounded context that owns the long-tail of cross-cutting tables.
The shared-kernel pieces (`outbox`, `idempotency`) live in `shared/*` because
they're BC-agnostic; everything else (attachments, import/export, metrics, ops
runs, jobs, dedup) lives in `ksma.operations.*`.

## User Stories

### Attachment
1. As any **user**, I want to upload a file with a typed `Attachment`
   (`media_type`, `size_bytes`, `content_hash`, `check_status`) so that I can
   attach evidence to an assessment or a payment proof.
2. As any **user**, I want my upload to be checked by type, size, and content
   hash (NFR07), so that a malicious file can't be executed as code.
3. As any **user**, I want my download to require X-A01 + parent-object rights
   + `check_status='approved'` at fetch time (R16, V19), so that a leaked URL
   doesn't bypass authorization.
5. As any **user**, I want files larger than 10 MB rejected at upload
   (NFR07, Q11 deferred).
6. As **OP**, I want an `Attachment` row to record its `retention_policy_id`
   so that retention runs know when to delete.

### Import
7. As **AC/FI**, I want to upload an import file (`ImportBatch.source_attachment_id`)
   and see a preview of row-level validation BEFORE commit (R18), so that I can
   fix issues without polluting data.
8. As **AC/FI**, I want the import to be atomic — all rows ready or no commit
   effect (R18, V20).
9. As **AC/FI**, I want my import mapping to be versioned (`rule_version`) and
   pinned, so that a row imported today can be re-validated tomorrow with the
   same rule.
10. As **AC/FI**, I want a row's outcome to be one of `valid`, `invalid`,
    `duplicate_candidate`, `committed`, so that I can filter.
11. As **AC/FI**, I want my import to write a `WorkItem` per failing row (or
    per batch if many) so that the data owner can correct the source.
12. As **AC/FI**, I want my import rights to NOT bypass per-row entity rules
    (R18): even if I have import permission, an `Enrollment` row that violates
    R07 (overlap) must be rejected.

### Export
13. As **OW/AC/FI/AU**, I want to create an `ExportJob` with `dataset_kind`,
    `scope`, `filters` (typed allowlist), `as_of` reference instant (R19).
14. As any **user**, I want my export rights re-checked at request, execution,
    and download (R19), so that revoked rights don't leak via stale links.
15. As **OW**, I want my export's filters to be from the typed allowlist
    (NOT free SQL), so that I cannot bypass `WHERE` clauses via URL params.

### Metric
16. As **OW**, I want to define a `MetricDefinition` (`metric_code`, `version_no`,
    `label`, `source_spec`, `time_basis`, `effective_range`, `approval`) so
    that the dashboard numbers are versioned and traceable.
17. As any **user**, I want my metric to compute correctly: numerator and
    denominator, status, timezone, divide-by-zero handling (R24, V24).

### Operational run
26. As **OP**, I want `OperationalRun(operation_kind, started_at, finished_at?,
    status, evidence)` to record backup, restore, restore_test, retention,
    and reconciliation runs (R23, V23).
27. As **OP**, I want my restore test to run in a separate environment and
    reconcile data + files + relations without replaying effects, so that
    V23 passes.
28. As **OP**, I want cross-tenant evidence to never leak: a backup of tenant A
    restored into a test env must not show tenant B's data (R23, V23).

### Background job + dedup
29. As the **outbox worker**, I want a `BackgroundJob` to retry with `attempt_count`
    and `next_attempt_at`, so that transient failures don't drop work.
30. As the **outbox worker**, I want a `ProcessedEvent(consumer_code, event_id,
    effect_ref, processed_at)` to dedup consumer effects (R20, V21).
31. As the **outbox worker**, I want my `dispatched` state to NOT claim all
    external effects completed — only the DB-side effect commits inside the
    worker transaction.

### Idempotency
32. As any **client**, I want my `Idempotency-Key` header on protected POSTs to
    produce the same response on replay (R21, V22).
33. As any **client**, I want a replay after rights revocation to return 403
    (no cached response) so that stale responses don't bypass authority (R03).

## Implementation Decisions

### Schema (already migrated, see `alembic/versions/0009_portal_operations.py`)
- `attachment(id, tenant_id, owner_ref JSONB, storage_key, original_name, media_type,
  size_bytes, content_hash?, check_status ('pending'|'quarantined'|'approved'|'rejected'|'deleted'),
  retention_policy_id?, …)`.
- `import_batch(id, tenant_id, requested_by JSONB, dataset_kind, source_attachment_id,
  mapping JSONB, scope JSONB, status ('uploaded'|'validating'|'needs_correction'|'ready'|'committed'|'failed'),
  commit_request_id?, counts JSONB, …)`.
- `import_row_result(id, tenant_id, import_batch_id, row_no, row_fingerprint, outcome
  ('valid'|'invalid'|'duplicate_candidate'|'committed'), errors JSONB?, target_ref?, source_reference?, …)`.
  Unique `(import_batch_id, row_no)`.
- `export_job(id, tenant_id, requester_account_id, dataset_kind, scope JSONB, filters JSONB,
  as_of, status ('queued'|'running'|'ready'|'failed'|'cancelled'|'expired'),
  result_attachment_id?, expires_at?, …)`.
- `metric_definition(id, tenant_id, metric_code, version_no, label, source_spec JSONB,
  time_basis, effective_range tstzrange, approval JSONB, …)`. Unique `(tenant_id, metric_code, version_no)`.
- `operational_run(id, tenant_id, operation_kind ('backup'|'restore_test'|'restore'|'retention'|'reconciliation'),
  environment, execution_subject_id, authorization_ref JSONB, started_at, finished_at?, status,
  evidence JSONB?, …)`.
- `background_job(id, tenant_id, event_id?, job_kind, execution_subject JSONB, scope JSONB,
  status ('queued'|'running'|'succeeded'|'retry_wait'|'failed'|'cancelled'),
  attempt_count, next_attempt_at?, result_ref?, …)`.
- `processed_event(id, tenant_id, consumer_code, event_id, effect_ref JSONB, processed_at, …)`.
  Unique `(tenant_id, consumer_code, event_id)`.
- `idempotency_record(id, tenant_id, request_subject JSONB, action_id, key_fingerprint,
  request_hash, status ('in_progress'|'succeeded'|'failed'), result_ref?, expires_at, …)`.
  Unique `(tenant_id, request_subject.id, action_id, key_fingerprint)`.

### File storage
- `AttachmentStorage` port with `LocalAttachmentStorage` (dev) and
  `S3AttachmentStorage` (prod) adapters. Default 10 MB.
- `storage_key` is server-generated, opaque. Download endpoint `GET /attachments/{id}/content`
  re-checks rights at fetch time.

### Import atomicity (R18)
- All-row-or-nothing: a single `BEGIN; SELECT FOR UPDATE on import_batch WHERE id=:bid;
  ... validate all rows; UPDATE import_batch SET status='committed'; COMMIT;`.
- Per-row results are written BEFORE commit; rollback removes them all.
- Duplicate candidates are flagged, never auto-merged.

### Export re-check at every step (R19, V19)
- The export job's `result_attachment_id` is generated after the run;
  the download endpoint re-checks `X-A01` on the `ExportJob` row at pickup time.
- No public signed URLs.

### Background worker loop
- `python -m ksma.shared.outbox.run` is the canonical loop. Uses
  `SELECT … FOR UPDATE SKIP LOCKED` to claim a batch, dispatches each event
  to subscribers, marks dispatched, writes `ProcessedEvent` per consumer.
- Retry policy: exponential backoff with `attempt_count`, `next_attempt_at`.

### Seams
- **Inbound**: every BC that produces notifiable events (finance, assessment,
  session, batch) plus the dashboard read for metrics.
- **Outbound**: operations depends only on `shared/*` and on `identity.domain.Account`
  for `execution_subject_id`.
- **ACL exports**: `operations.acl.attachments_for_owner(owner_ref, requester_id)`,
  `operations.acl.exports_for_account(account_id)`.

## Testing Decisions

What makes a good test:
- V19: download a `quarantined` file → 403; download after rights revoked → 403.
- V20: 100-row import with one cross-tenant row → 0 rows committed (atomic).
- V21: outbox event consumed twice → one `InternalNotification` row + one `ProcessedEvent` row.
- V22: same `Idempotency-Key` with different body → 409; with same body → cached response;
  after revocation → 403.
- V23: restore tenant A from shared backup into isolated test env → A's data +
  files + balances match; B's data not present in the restored env.
- V24: denominator capacity zero → `not_applicable`, not `0`; unique participants
  counted once even if enrolled in two classes.

## Out of Scope

- Backup infrastructure itself (S3 snapshots, Barman, etc.) — only the
  evidence-recording flow is in scope.
- Retention enforcement automation (Q11 deferred).
- BI dashboards beyond the metric definitions.

## Further Notes

- The existing `src/ksma/operations/` folder has minimal stubs. The schema is
  complete; the Python code needs to be written.
- `shared/outbox`, `shared/idempotency`, `shared/persistence`, `shared/events`
  are mostly implemented (~2,000 lines total in `shared/*`). This spec
  covers the BC-specific pieces.
- Remaining work:
  1. Implement `ksma.operations.{domain,infrastructure,application,interface}`.
  2. Wire `AttachmentStorage` port and Local/S3 adapters (the pyproject.toml
     already lists `minio`).
  3. Implement the import preview-and-commit flow with atomic transaction.
  5. Add integration tests for V19, V20, V21, V22, V23, V24.