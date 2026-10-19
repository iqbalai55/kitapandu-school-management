# 09: Operations slice — attachments, import preview, export, metric, ops run

**What to build:** A complete operations surface: file attachments with type/size/content checking and rights-checked download; bulk import with preview + atomic commit; export with rights re-check at request / execution / download; metric definitions; operational-run evidence for backup and restore; the `AttachmentStorage` port and Local/S3 adapters. After this ticket, V19 (download after revocation) and V20 (atomic import) are demoable.

**Blocked by:** 00, 01, 02 (identity for account/grant)

**Status:** ready-for-agent

- [ ] `AttachmentStorage` port implemented in `ksma.shared.attachments` with `LocalAttachmentStorage` (writes under `/var/kitapandu/attachments/<tenant_id>/<attachment_id>/<filename>`) and `S3AttachmentStorage` (MinIO in docker-compose, S3 in prod).
- [ ] `POST /api/v1/attachments` accepts a `multipart/form-data` upload with `owner_ref` (typed `ObjectRef`), returns `{attachment_id, check_status='pending'}` after writing the file with size + content_hash; rejects files > 10 MB (NFR07, Q11 deferred); rejects content types outside the allowlist.
- [ ] `POST /api/v1/attachments/{id}/scan` runs the (stub) content-type check + size check + AV scan stub; transitions `pending → approved | quarantined`.
- [ ] `GET /api/v1/attachments/{id}/content` re-checks `X-A01` + parent-object rights + `check_status='approved'` at fetch time (R16, V19); streams the bytes; refuses quarantined or revoked content even if the URL is leaked.
- [ ] `POST /api/v1/imports/preview` (OPS-A01) accepts an `Attachment` (source file) + `dataset_kind` + `mapping` (column map + rule_version); runs row-level validation in a dry-run; returns per-row `outcome ∈ {valid, invalid, duplicate_candidate}` with a typed `ValidationErrorList` (code + field_path + safe message); never writes business rows.
- [ ] `POST /api/v1/imports/commit` (OPS-A02) requires `commit_request_id` (idempotency); in one transaction validates each row's source version + mapping + scope + column against the current state; on any cross-tenant row → 0 rows committed (R18, V20); on success writes `ImportBatch(status='committed')` + `ImportRowResult(target_ref)` per row; requires `Idempotency-Key` header.
- [ ] `POST /api/v1/exports` (OPS-A04) accepts `dataset_kind`, `scope`, typed `filters` (allowlist, NOT free SQL), `as_of`; queues `ExportJob(status='queued')`.
- [ ] `POST /api/v1/exports/{id}/run` (operator) marks `running → ready | failed` and writes the result `Attachment`; rights re-checked at execution time.
- [ ] `GET /api/v1/exports/{id}` returns the job including the result `Attachment`; rights re-checked at download time (R19, V19).
- [ ] `POST /api/v1/indicators` (X-A08) creates a `MetricDefinition` with `metric_code`, `version_no`, `label`, `source_spec` (typed), `time_basis`, `effective_range`, `approval`; uniqueness on `(tenant_id, metric_code, version_no)`.
- [ ] `GET /api/v1/indicators/{id}` returns the metric definition + a fresh computation result using the typed `MetricSource` (numerator + denominator + status + timezone).
- [ ] `POST /api/v1/ops/runs` (X-A05) records `OperationalRun(operation_kind, environment, execution_subject_id, authorization_ref)` for backup / restore / restore_test / retention / reconciliation; refuses `restore` outside a non-prod environment (V23); refuses `restore_test` outside an isolated env.
- [ ] `PATCH /api/v1/ops/runs/{id}` records `finished_at` + `evidence` (typed) on terminal status.
- [ ] Integration tests for: V19 download after rights revocation refused, V20 100-row import with one cross-tenant row → 0 rows committed, V23 restore test only in non-prod env, V24 denominator zero returns the actor "not applicable" not "0".