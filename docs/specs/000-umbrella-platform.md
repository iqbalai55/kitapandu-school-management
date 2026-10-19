# Spec 000 — KSMA Platform Umbrella

> Cross-cutting concerns that span every bounded context: tenancy, RBAC, money, outbox,
> idempotency, audit, auth, attachments, observability. Each per-BC spec (`001`–`009`)
> references these primitives by name; this umbrella is their contract.

## Problem Statement

KSMA serves many independent schools (`pelanggan`/tenants), each with their own units,
staff, participants, money, and reports. A single bug in access control or money math
in any one bounded context could leak another tenant's data, corrupt invoices, or
destroy audit history. The team needs a shared foundation that makes correct behaviour
the default and makes incorrect behaviour loud, before any business logic is written.

## Solution

A single FastAPI service (`ksma.main`) with nine bounded contexts under `/api/v1`,
backed by PostgreSQL 16 with row-level security, an Argon2id + JWT auth layer, an
in-DB outbox + worker for cross-context events, and a versioned action registry for
RBAC. Every state-changing route is protected by the same five primitives:
`TenantContext`, `require_action`, `@audited`, `Idempotency-Key`, `OutboxEvent`.

## User Stories

These span the whole product, not a single BC.

1. As an **operator platform (OP)**, I want to onboard a new `pelanggan` (tenant) with its
   first unit, so that the new school can begin registering participants.
2. As an **operator platform (OP)**, I want a second test tenant to exist, so that I can
   prove tenant isolation (V01) without polluting the real customer's data.
3. As a **pengelola akses (AM)**, I want to invite an admin and assign them the `AC` role
   in my tenant, so that the new admin can manage participants without me touching
   shared platform credentials.
4. As a **pimpinan (OW)**, I want to read the audit trail of important changes in my unit,
   so that I can answer "who changed what, when, and why" without being able to edit it.
5. As a **pemeriksa (AU)**, I want to read audit rows for the events I'm mandated to
   review, so that I can investigate discrepancies without seeing unrelated tenants.
6. As any **staff user**, I want my session to be revoked the moment my rights change, so
   that I can't keep using permissions that were taken away (R03).
7. As any **staff user**, I want my JWT to expire in 15 minutes and refresh in 7 days, so
   that a stolen token has limited blast radius.
8. As any **user**, I want to recover my account with a time-bounded emailed token, so that
   I can regain access without an admin intervention.
9. As **finance (FI)**, I want every currency field stored as integer minor units in a
   single currency, so that rounding never produces phantom money (R12).
10. As **finance (FI)**, I want every allocation to run inside a `SELECT … FOR UPDATE`
    transaction, so that two simultaneous allocations can never both succeed against the
    same installment (V15, V16).
11. As any **service caller**, I want my `POST` to be retried safely with the same
    `Idempotency-Key` header, so that network retries don't create duplicate payments
    (R21).
12. As a **portal user**, I want a denied request to return a generic 403 with no object
    payload, so that I can't enumerate participants by trying random IDs.
13. As any **user**, I want every important change to be wrapped in an outbox event, so
    that downstream notifications, audit projections, and integration jobs can be
    triggered without distributed transactions (ADR06).
14. As any **user**, I want every important change to record `actor`, `time`, `object`,
    `action`, `reason`, and `prior_version`, so that corrections can be traced (R22, BR07).
15. As any **staff**, I want my file uploads checked by type, size, and content hash, so
    that a malicious file can't be executed by the app (NFR07).
16. As any **staff**, I want a `correlation_id` on every request propagated to every log
    line and every error response, so that support can find the full trace in seconds.
17. As an **operator platform (OP)**, I want a `ServicePrincipal` row per technical job
    (backup, retention, outbox worker), so that audit rows record the right actor for
    automated work.
18. As a **pengelola akses (AM)**, I want a `SupportGrant` row to time-bound and scope a
    platform operator's read-only access to a customer's data, so that support never has
    silent unrestricted access.
19. As an **operator platform (OP)**, I want every tenant's data isolated by Postgres RLS
    on top of application-layer scoping, so that a forgotten `WHERE` clause still cannot
    leak across tenants.
20. As a **developer**, I want one `Money` value object and one `Currency` enum, so that
    money math is unit-consistent everywhere.
21. As a **developer**, I want one `ActionCode` enum covering all 73 codes from the access
    matrix, so that FastAPI routes declare `Depends(require_action(ActionCode.FIN_A05))`
    and the OpenAPI spec is the authoritative permission surface.
22. As a **developer**, I want an `ObjectRef` shared kernel type, so that cross-BC
    references are typed and validated at write-time (R01).
23. As a **developer**, I want one `Clock` port and one `Now()` function, so that tests
    can pin time deterministically (freezegun).
24. As a **developer**, I want a single `session_scope()` async context manager that sets
    the RLS GUC at the start of every transaction, so that RLS cannot be bypassed by a
    forgotten setup call.

## Implementation Decisions

### Stack
- **Language**: Python 3.12.
- **Framework**: FastAPI 0.115+, uvicorn[standard].
- **ORM**: SQLAlchemy 2.0 async with **raw-SQL repositories** (existing pattern).
  Models in `*_infrastructure.models` for documentation/typing; repositories use
  `sqlalchemy.text()` with explicit `tenant_id` filters plus RLS.
- **DB**: PostgreSQL 16. Required extensions: `pgcrypto` (`gen_random_uuid`), `pg_stat_statements`,
  `btree_gist` (for `tstzrange` exclusion constraints), `citext` (case-insensitive codes).
- **Migrations**: Alembic 1.14, raw-SQL `op.execute(...)` per migration. `target_metadata = None`
  (existing pattern). CI drift check via a separate `alembic check` script.
- **Validation**: Pydantic v2 with `ConfigDict(from_attributes=True)` for read schemas.
- **Settings**: `pydantic-settings` 2.6, prefix `KITAPANDU_`, env file `.env`.
- **Logging**: `structlog` JSON, `correlation_id` propagated from
  `X-Correlation-Id` request header (generated if missing).
- **Lint**: `ruff` (E,F,I,B,UP,SIM,RUF,ASYNC,S,PL), `mypy --strict-equality` initially.
- **Object storage**: MinIO (S3-compatible) in dev, S3 in prod. The dev bucket
  `ksma-attachments` is created by `minio-init` in `docker-compose.yml`.

### Tenancy
- One row per `Tenant` (UUID PK). `EducationUnit.tenant_id` FK + `(tenant_id, unit_code)` unique.
- Every T/U table has `tenant_id UUID NOT NULL REFERENCES tenant(id)`.
- Every T/U table has `ENABLE ROW LEVEL SECURITY; FORCE ROW LEVEL SECURITY;` + policy
  `USING (ksma_bypass_rls() OR tenant_id = ksma_current_tenant())`.
- `ksma_current_tenant()` and `ksma_bypass_rls()` are SECURITY DEFINER plpgsql functions
  reading `current_setting('app.current_tenant', true)` and `current_setting('app.bypass_rls', true)`.
- Application layer: `session_scope()` async context manager runs
  `SET LOCAL app.current_tenant = :tid;` (or `SET LOCAL app.bypass_rls = 'on';` for seeds/migrations).
- `TenantContext` is a Pydantic model `account_id, membership_id, tenant_id, unit_id, role_code, scopes`.
  It is built by `tenant_context_dep()` from the JWT and stored on `request.state`.
- Every route that touches data declares `Depends(tenant_context_dep)`.
- Default-deny: routes that don't request context get 401; routes that request context but
  lack an action grant get 403 with **no** object payload (no name leakage, per NFR08).

### RBAC
- `ActionCode(str, Enum)` — 73 codes from KSMA Access Control Matrix v1.0, grouped by
  module prefix (PLT/REG/CLS/SES/ASM/FIN/PRT/OPS/X).
- `AccessAssignmentRow` carries `action_ids: ARRAY(String)` (CodeList) + JSONB `scope`
  with optional `unit_ids`, `participant_id`, `column_mask`, `object_kind`.
- `can(actor, action_id, target) -> Decision` evaluates **one grant** at a time
  (whole-grant, R02). No splicing scope from one grant with action from another.
- FastAPI dependency: `require_action(action_code, *, scope_kind=None)` returns
  `TenantContext` or raises `AccessDeniedError`.
- Column-mask enforcement happens in `mask_response(payload, actor, object_ref)`,
  called from the response layer for routes flagged with `requires_column_mask=True`.

### Money
- `Money(amount: int, currency: Currency)` value object.
- `Currency` enum: `IDR` is the only pilot currency (R12, Q04).
- Storage: `NUMERIC(18, 0)` (no fractional IDR; no float anywhere).
- Allocations run inside a single transaction with `SELECT … FOR UPDATE` on both
  the `verified_payment` row and the `installment` row, then re-check
  `available = recognized - reversals - effective_allocations` and `remainder = due_amount - credits - effective_allocations`
  before INSERT (R14).
- Money functions return `Money` or raise `ValidationError` / `StateConflictError`.
- Idempotency on allocation uses `IdempotencyRecord` (R21).

### Outbox
- `OutboxEvent` table (`event_type, source_ref, source_version, payload, occurred_at,
  dispatch_state, correlation_id`).
- `enqueue(session, event, aggregate_ref)` writes the row in the same transaction as the
  business mutation.
- Worker: `python -m ksma.shared.outbox.run` polls `WHERE dispatch_state='pending'
  ORDER BY occurred_at FOR UPDATE SKIP LOCKED LIMIT :n` every `OUTBOX_POLL_INTERVAL_SECONDS`,
  dispatches each to the in-process `bus`, marks `dispatch_state='dispatched'`,
  and writes a `ProcessedEvent` row to dedup (R20).
- Consumers register via `bus.subscribe(event_type, handler)`; handlers run inside their
  own transaction; failure re-queues with `dispatch_state='failed'`.

### Idempotency
- Protected POSTs (`/payments`, `/allocations`, `/imports/commit`, `/sessions/{id}/close`,
  `/reports/{id}/publish`) read `Idempotency-Key` header.
- `IdempotencyRecord` row keyed on `(tenant_id, requester_account_id, action_id, key_fingerprint)`.
- Same key + same `request_hash` → return cached `result_ref`. Same key + **different**
  hash → 409 with code `IDEMPOTENCY_CONFLICT`. First request with new key → execute,
  persist, return. Replay after rights revoked → 403 (no stale result).

### Audit
- `AuditRecord` is **append-only**. No service method may UPDATE or DELETE except the
  retention `ServicePrincipal`.
- `@audited(action_id, *, reason_required=False, change_ref_builder=None)` decorator
  wraps service functions; inside the same DB transaction it writes:
  `actor={account_id|service:code}, action_id, object_ref=ObjectRef(kind,id),
   occurred_at=now(), change_ref={prior_row_version, masked_diff}, reason?,
   authorization_ref=AccessAssignment(id)|SystemJob(code), correlation_id`.
- Reads (`GET /audit-trail`) require `ActionCode.PLT_A07` and are scoped by tenant + unit.

### Auth
- **Hashing**: `argon2-cffi` default parameters, per-row salt.
- **Login**: `POST /auth/login` returns `{access_token, refresh_token, expires_in}`.
  Access TTL 900s, refresh TTL 604800s. Refresh tokens stored as SHA-256 of the
  opaque string in `RefreshToken`; rotated on every refresh (`revoked_reason='rotated'`,
  `replaced_by_id` points at the new row).
- **JWT claims**: `sub=account_id, membership_id, tenant_id, role_code, authz_version,
  iat, exp, iss, aud`. Mismatch on `authz_version` forces re-auth via refresh.
- **Recovery**: `POST /auth/recover/start` creates `RecoveryToken` with 30-min TTL;
  `POST /auth/recover/complete {token, new_password}` consumes it, rotates
  `authz_revision`, returns fresh token pair.
- **Logout**: `POST /auth/logout` revokes the current refresh token.
- `Account.authz_revision` increments on any change to the account's `Membership`,
  `AccessAssignment`, `SupportGrant`, or the account itself.

### Attachments
- `Attachment` row stores `storage_key` (server-generated, opaque, never a URL),
  `original_name` (sanitised display name), `media_type`, `size_bytes`,
  `content_hash`, `check_status` (`pending`/`quarantined`/`approved`/`rejected`/`deleted`).
- Upload goes to `LocalAttachmentStorage` (dev) or `S3AttachmentStorage` (prod) behind
  the `AttachmentStorage` port. Default limit 10 MB (NFR07).
- Download endpoint `GET /attachments/{id}/content` re-checks `X-A01` + parent-object
  rights + `Attachment.check_status='approved'` **at fetch time** (R16).
- No public signed URLs (R16 / V19). The download stream goes through the API.

### Observability
- `structlog` JSON to stdout. Every log line has `tenant_id?`, `unit_id?`, `actor_id?`,
  `action_id?`, `correlation_id`.
- Request middleware reads `X-Correlation-Id` (or generates UUID4), binds it on the
  contextvar, echoes it on the response header.
- Errors surface `correlation_id` to the client in the standard error envelope
  `{code, message, field_path?, correlation_id}`.
- OTel SDK optional (`OTEL_SDK_ENABLED=false` by default).

### OpenAPI / API style
- FastAPI auto-generated OpenAPI at `/openapi.json` and Swagger UI at `/docs`.
- Versioned URL prefix `/api/v1`.
- Errors: `{code, message, field_path?, correlation_id}`. Common codes: `INVALID_VALUE`,
  `REQUIRED_FIELD`, `REFERENCE_SCOPE_MISMATCH`, `STATE_CONFLICT`, `VERSION_CONFLICT`,
  `CAPACITY_EXCEEDED`, `ACCESS_DENIED`, `IDEMPOTENCY_CONFLICT`, `POLICY_NOT_CONFIGURED`.
- Optimistic concurrency via `If-Match` header carrying `row_version` on `Session`,
  `Enrollment`, `ReportPublication`. 409 with current version on conflict.
- `Idempotency-Key` header on protected POSTs (per ADR-0001 R21).

### Testing seams
- Unit tests (`tests/unit/`) — pure-Python domain tests, no DB.
- Integration tests (`tests/integration/`) — `testcontainers-python` ephemeral
  Postgres 16 + httpx.AsyncClient + full SQLAlchemy. Two seeded tenants
  (`Vaneta DEMO`, `Lembaga Uji DEMO`) for V01.
- Acceptance tests (`tests/e2e/scenarios/v01_cross_tenant.py` … `v24_metric_denominator.py`)
  — pytest scenarios mapped 1:1 to the data dictionary `V01–V24`.
- Seed: `python -m ksma.scripts.seed_vaneta_demo` is idempotent, records an
  `OperationalRun` row.

## Testing Decisions

What makes a good test for the platform layer:
- Asserts on **API response shape and DB-visible state**, never on log strings,
  private attributes, or internal helper names.
- Cross-tenant isolation: asserts that an `AC` from tenant A gets 403 with no payload
  when accessing tenant B's resources (V01).
- Money: asserts integer arithmetic, never float. Asserts that an over-allocation
  returns 409 and the installment remainder is not negative (V15, V17).
- Idempotency: asserts that replaying a protected POST with the same key returns the
  cached response, and that replaying with a different body returns 409.
- Audit: asserts that every protected POST writes one `AuditRecord` row with the
  right `actor`, `action_id`, `object_ref`, and `correlation_id`.
- Time: tests use `freezegun` to pin `now()` for `validity`, `effective_range`,
  and overdue checks.

## Out of Scope

- WhatsApp / SMS / email provider integration (Q09 pending).
- Payment gateway integration (P1 per PRD §3.9).
- Public certificate verification link (Q12 pending — pilot keeps certificates private).
- BI dashboards beyond the `MetricDefinition` schema.
- WebAuthn / passkeys / MFA (Q08).
- Microservices split. KSMA is a single FastAPI service for pilot.

## Further Notes

- **Pre-existing scaffolding** in `kitapandu-backend/` already implements most of this:
  the 9 Alembic migrations, the shared kernel (`auth`, `rbac`, `money`, `tenancy`,
  `outbox`, `idempotency`, `errors`, `events`, `time`, `persistence`), the
  application/router code for `participant`, `program`, `batch`, `session`,
  `finance`, `assessment`. Specs 001–008 reference the existing modules and call
  out only what is missing or where the existing implementation diverges from the
  ADRs.
- The `reporting` and `portal` contexts need their missing infrastructure files
  completed (see specs 007 and 008).
- The seed script is a thin 6-line placeholder; spec 009 covers the real seed.
- This umbrella spec plus specs 001–009 are published together; they will be
  iterated after the first pilot UAT run.