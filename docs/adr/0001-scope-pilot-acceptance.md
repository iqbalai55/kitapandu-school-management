# ADR-0001: Pilot-Acceptance Scope with DDD Layout

- **Status**: Accepted
- **Date**: 2026-10-20
- **Context**: KitaPandu School Management backend, KSMA v1.0 pilot for Vaneta Maju Berdaya.

## Decision

- **Scope (Q1 = c)**: Implement all P0-marked requirements from `KSMA_PRD_v1_0` in a single backend
  service. All 55 entities are modelled and migrated; stubs are *not* acceptable for P0 surfaces
  (PLT, REG, CLS, SES, ASM, FIN, PRT, OPS). P1 surfaces (WhatsApp gateway, BI dashboards, public
  certificate verification, internships) remain out of scope per PRD §3.9.
- **Stack (Q2 = a)**: Python 3.12 + FastAPI 0.115 + SQLAlchemy 2.0 (async) + Alembic + asyncpg +
  Pydantic v2 + pytest + httpx.AsyncClient + structlog + uv.
- **Tenancy (Q3 = a)**: App-layer scope resolution via `TenantContext` dependency + Postgres RLS
  policy on every T/U table keyed on `current_setting('app.current_tenant', true)::uuid`.
- **RBAC (Q4 = a)**: Centralised `ACTION_REGISTRY` dict of 73 action codes with default grants,
  scope kinds, and column-mask hints. Single `can(actor, action_id, target)` evaluator that returns
  `Decision.ALLOW | Decision.DENY(reason)`. R02 whole-grant evaluation in one pass.
- **Money (Q5 = a)**: `Money(amount: int, currency: Currency)` value object, stored as
  `NUMERIC(18, 0)` in Postgres with `CHECK (amount >= 0)`. Allocation in `SELECT … FOR UPDATE`
  transaction with available-funds + installment-reminder recheck; 409 on race.
- **Async (Q6 = a)**: `OutboxEvent` + `IdempotencyRecord` tables; `python -m ksma.outbox.run` worker
  CLI; protected POSTs require `Idempotency-Key` header.
- **Auth (Q7 = a)**: Argon2id password hashing; JWT access token (15 min) with `authz_version`
  claim; hashed refresh token (7 days) in `RefreshToken` table; time-bounded `AccountRecoveryToken`;
  session revocation by incrementing `Account.authz_revision`.
- **API (Q8 = a)**: REST + OpenAPI 3.2; `/api/v1` prefix; `{ code, message, field_path?, correlation_id }`
  error shape; `If-Match` / `ETag` on session/enrollment/report; `Idempotency-Key` header on protected
  writes.
- **Layout (Q10 = DDD)**: Domain-Driven Design with bounded contexts per PRD module family; shared
  kernel for cross-context concepts (money, audit, outbox, idempotency, attachment).

## Bounded contexts (from PRD module codes)

| BC | PRD module | Owns |
|---|---|---|
| **Platform** (`platform`) | PLT01–04 | `Tenant`, `EducationUnit`, `Account`, `Membership`, `AccessAssignment`, `ServicePrincipal`, `SupportGrant`, `PolicyVersion`, `Branding` |
| **Identity** (`identity`) | PLT03 + cross-cutting | `Account`, `RefreshToken`, `AccountRecoveryToken`, `auth_subject` namespace |
| **Access** (`access`) | PLT03, OPS-A05/06 | `AccessAssignment`, `SupportGrant`, `AuditRecord`, grant resolver, action registry |
| **Participant** (`participant`) | REG01–04, PLT01 | `Participant`, `RelatedParty`, `ParticipantAccountLink`, `ParticipantRelation`, `Application`, `Enrollment`, `EnrollmentChange` |
| **Program** (`program`) | CLS01–04 | `Program`, `ProgramVersion`, `AcademicPeriod`, `Batch`, `TeachingAssignment` |
| **Session** (`session`) | SES01–06 | `Session`, `SessionRevision`, `Attendance`, `TeachingJournal`, `SessionCompletion`, instructor substitution |
| **Assessment** (`assessment`) | ASM01–04 | `RubricVersion`, `RubricIndicator`, `Assessment`, `AssessmentResult`, `AssessmentEvidence`, `ReportPublication`, `CompletionDecision`, `Certificate` |
| **Finance** (`finance`) | FIN01–04, PRT02 | `BillingParty`, `Invoice`, `InvoiceLine`, `Installment`, `PaymentSubmission`, `VerifiedPayment`, `PaymentAllocation`, `Receipt`, `FinancialAdjustment` |
| **Portal** (`portal`) | PRT01–04 | `ServiceRequest`, `InternalNotification`, `CommunicationPreference`, portal-scoped read models |
| **Operations** (`operations`) | OPS01–04 | `ImportBatch`, `ImportRowResult`, `ExportJob`, `MetricDefinition`, `OperationalRun`, `Attachment` |
| **Outbox / Async** (`outbox`) | ADR06, R20, R21 | `OutboxEvent`, `BackgroundJob`, `ProcessedEvent`, `IdempotencyRecord`, worker CLI |

## Shared kernel (`app/core/`)

- `core.auth` — Argon2id hashing, JWT encode/decode, refresh rotation.
- `core.money` — `Money`, `Currency`, allocation invariants.
- `core.errors` — `DomainError`, `ValidationError`, error response shape.
- `core.audit` — `AuditRecorder` port; `core.audit.persistor` adapter writes `AuditRecord`.
- `core.outbox` — `OutboxPort`; in-DB adapter; transactional enqueue.
- `core.idempotency` — header parsing, `IdempotencyRecord` lookup, replay-or-fail.
- `core.tenancy` — `TenantContext` (FastAPI dep), `with_tenant_scope` transaction decorator, RLS helpers.
- `core.attachments` — file metadata, quarantine lifecycle.
- `core.time` — `Clock` port for testable Instants.
- `core.events` — domain event names per BC.

## Consequences

- **Positive**: ADR traceability is structural — every PRD module maps to exactly one BC; every
  requirement ID lands in exactly one package.
- **Positive**: All 55 entities exist as schemas on day one, so Alembic migration history starts clean
  and there is no painful "split the monolith" rewrite later.
- **Cost**: DDD adds a layer of indirection. We will keep aggregate boundaries tight (most BCs have
  3–6 aggregates, no aggregate crosses BCs).
- **Risk**: Cross-BC flows (placement → invoice work-item) must go through outbox + ACL, never direct
  table access. We will enforce this with import-linter.
- **Risk**: We are skipping the document's own API spec deliverable. We will publish an OpenAPI doc
  per release instead.

## Deferred decisions

The PRD's open decisions (Q01–Q14) remain open in the model:
- Tariffs, installment counts, passing thresholds are NOT silent defaults. They live in `PolicyVersion`
  records with explicit `status = draft` until a process owner approves them.
- Q08 (auth method), Q11 (file size/retention), Q12 (certificate numbering), Q04 (refund flow) are
  stubbed behind `NotImplementedError` returns and `501` HTTP responses, with an ADR pointer in
  the error payload.