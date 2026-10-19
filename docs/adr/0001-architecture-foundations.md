# ADR 0001 — Backend architecture foundations (KitaPandu School Management)

- **Status:** Accepted
- **Date:** 2026-10-20
- **Decision owners:** Product, Engineering
- **Source documents:** KSMA PRD v1.0, Architecture & Data Model v1.0, Access Control Matrix v1.0, Data Dictionary v1.0, UX Blueprint v1.0.

## Context

KitaPandu School Management (KSMA) is a multi-tenant school management platform piloted at Vaneta Maju Berdaya. The v1.0 documents define 55 entities, 13 roles, 73 permissioned actions, 24 cross-data rules (R01–R24), 24 validation scenarios (V01–V24), 10 non-functional requirements (NFR01–NFR10), and 14 open policy decisions (Q01–Q14). The product must enforce tenant + unit scoping on every read/write, money in exact arithmetic with no negative balance, idempotent allocation, append-only audit, and outbox-driven background work.

## Decision

We are building a single FastAPI backend service that implements the full pilot-acceptance scope (PRD P0) end to end. Ten decisions taken together:

1. **Stack** — Python 3.12, FastAPI, SQLAlchemy 2.x async, Alembic, asyncpg, Pydantic v2, pytest.
2. **Persistence** — PostgreSQL 16. Money as `NUMERIC(18,0)` IDR; partial unique indexes `(tenant_id, code)`; `SELECT … FOR UPDATE` for capacity races.
3. **Multi-tenancy** — Application-layer scoping (resolver sets `current_tenant`, `current_unit`, `actor`) + Postgres Row-Level Security as defence in depth.
4. **Authorisation** — Action code registry (`ACTION_REGISTRY`, 73 codes) + whole-grant evaluator implementing R02/R03. FastAPI `Depends(require_action(...))` on every protected route.
5. **Money** — `Money(amount: int, currency: Currency)` value object; allocations execute in one transaction with available-funds + installment-remainder checks (R14, R15); negative-balance impossible.
6. **Outbox & idempotency** — `OutboxEvent` written with the business transaction; `IdempotencyRecord` per (tenant, requester, action, key); CLI worker drains outbox and writes `ProcessedEvent` for consumer dedup (R20, R21).
7. **Auth** — Argon2id password hashing; JWT access token (15 min) + hashed refresh token (7 days) + time-bounded recovery token; `authz_revision` claim invalidates tokens on rights change.
8. **API** — REST + OpenAPI 3.2 under `/api/v1`; error shape `{ code, message, field_path?, correlation_id }`; `Idempotency-Key` header on protected POSTs; `If-Match`/`ETag` for optimistic concurrency.
9. **Scope** — Pilot-acceptance: all 55 entities implemented end to end across PLT, REG, CLS, SES, ASM, FIN, PRT, OPS.
10. **Layout** — Domain-Driven Design. Each bounded context is a Python package with `domain/` (entities, value objects, domain events), `application/` (use cases, services), `infrastructure/` (SQLAlchemy models, repositories), `interface/` (FastAPI routers, Pydantic schemas). Cross-cutting `shared/` adds `auth`, `money`, `outbox`, `idempotency`, `rbac`, `errors`, `events`.

## Layout

```
kitapandu-backend/
├── pyproject.toml
├── alembic.ini
├── docker-compose.yml                # Postgres + API + worker
├── .env.example
├── alembic/
│   ├── env.py
│   └── versions/                     # 0001_initial.py, 0002_…, 0008_…
├── src/ksma/
│   ├── main.py                       # FastAPI app factory
│   ├── settings.py                   # pydantic-settings
│   ├── shared/                       # cross-cutting
│   │   ├── auth/                     # argon2, jwt, refresh, recovery
│   │   ├── money/                    # Money value object
│   │   ├── tenancy/                  # TenantContext, RLS helpers
│   │   ├── rbac/                     # ACTION_REGISTRY, grant resolver
│   │   ├── idempotency/              # IdempotencyRecord, header parser
│   │   ├── outbox/                   # OutboxEvent writer, CLI worker
│   │   ├── errors/                   # domain errors, HTTP mapping
│   │   ├── events/                   # DomainEvent base, dispatcher
│   │   ├── time/                     # Clock, tz Asia/Jakarta
│   │   └── persistence/              # AsyncSession, base model, RLS
│   ├── identity/                     # Bounded context: Tenant, Unit, Account, Membership, AccessAssignment, SupportGrant, ServicePrincipal, AuditRecord
│   ├── participant/                  # Participant, RelatedParty, ParticipantAccountLink, ParticipantRelation
│   ├── program/                      # Program, ProgramVersion, AcademicPeriod, RubricVersion, RubricIndicator, PolicyVersion
│   ├── batch/                        # Batch, Application, Enrollment, EnrollmentChange, TeachingAssignment
│   ├── session/                      # Session, SessionRevision, Attendance, TeachingJournal, SessionCompletion
│   ├── assessment/                   # Assessment, AssessmentResult, AssessmentEvidence
│   ├── reporting/                    # ReportPublication, CompletionDecision, Certificate
│   ├── finance/                      # Invoice, InvoiceLine, Installment, PaymentSubmission, VerifiedPayment, PaymentAllocation, Receipt, FinancialAdjustment
│   ├── portal/                       # ServiceRequest, InternalNotification, CommunicationPreference
│   └── operations/                   # Attachment, ImportBatch, ImportRowResult, ExportJob, MetricDefinition, OperationalRun, IdempotencyRecord, BackgroundJob, ProcessedEvent
└── tests/
    ├── conftest.py                   # test DB, RLS off, factory
    ├── unit/                         # domain unit tests
    ├── integration/                  # API + DB
    └── e2e/                          # vertical scenarios V01–V24
```

## Consequences

### Positive

- One service, one DB, one migration history. Easier to reason about consistency and tenant isolation in a pilot.
- DDD layout makes the cost of change per bounded context visible; cross-cutting concerns live in `shared/` and are explicit imports.
- RLS + app-layer scoping catch tenant leaks in two places (defence in depth, OWASP R1).
- Money invariants live in the value object and the transactional allocation endpoint — both unit-testable.
- Outbox makes notifications, audit projections, and reconciliation jobs consistent with the business write.

### Negative / Risks

- One service means one deploy unit; if Vaneta grows beyond pilot we may need to split by bounded context (this is the standard CQRS-strangler path).
- RLS adds a small latency overhead and complicates local dev unless we run with `SET app.bypass_rls = 'true'` for migration tooling.
- Whole-grant evaluation (R02) means we cannot optimize access checks by indexing on `(action_id)` alone — we must evaluate the full grant set per request.
- Q01–Q14 open decisions are still policy gaps. We will record them as `PolicyVersion.draft` placeholders and gate real-money paths behind their `published` status.

### Follow-ups

- ADR0002 — Outbox worker dispatch strategy.
- ADR0003 — File storage backend (S3-compatible vs local).
- ADR0004 — Time/zone discipline and clock injection.
- ADR0005 — Test strategy: contract tests for the API, scenario tests V01–V24, separate-DB recovery drill (R23).

## Implementation gotchas (verified Oct 2026)

- **SQLAlchemy 2.x async + RLS**: use `async_sessionmaker(expire_on_commit=False, autoflush=False)`; on each request, open the session, then `SET LOCAL app.current_tenant = :tid` via `await session.connection().execute(text(...))`. The session's autobegin keeps that GUC alive until commit/rollback. Never use `event.listen` for cursor-execute hooks — asyncpg uses different code paths.
- **RLS bypass for tests / migrations**: add `current_setting('app.bypass_rls', true) = 'true'` as a short-circuit at the top of every policy. Run tests with `SET app.bypass_rls = 'true'` on a dedicated role. Don't grant `BYPASSRLS` to the app role.
- **Exclusion constraints**: enable `CREATE EXTENSION btree_gist`. Use `ExcludeConstraint((col, "="), (range_col, "&&"), where="status='active'")`. Alembic `op.create_exclude_constraint(...)` is supported.
- **Argon2id**: `argon2-cffi>=25` directly, not passlib. `PasswordHasher().hash/.verify`; catch `VerifyMismatchError`; call `check_needs_rehash` at login.
- **JWT**: `pyjwt>=2.10` with `algorithms=["HS256"]` explicit, `aud` + `iss` claims required, `options={"require": [...]}` to reject incomplete tokens.
- **MinIO**: presigned URLs are NOT authorization — always re-check `X-A01` + parent-object permission on the server before issuing the URL, and again before serving the object. `presigned_put_object` does not enforce `Content-Type`; persist the declared type in `Attachment.media_type`.
- **pydantic-settings**: use `SettingsConfigDict(env_file=".env", env_prefix="KITAPANDU_")` — the inner `class Config:` style still works but is deprecated from 2.6+.
- **Outbox worker**: `SELECT … FOR UPDATE SKIP LOCKED LIMIT N` + UPDATE RETURNING in one statement; publish-then-mark for at-least-once with idempotent consumers (we use `ProcessedEvent` dedup per R20).

## Traceability

| PRD requirement | Where implemented |
|---|---|
| PLT01..04 | `identity` bounded context + `shared/auth`, `shared/tenancy` |
| REG01..04 | `participant` + `batch` bounded contexts |
| CLS01..04 | `program` + `batch` + `session` |
| SES01..04 | `session` bounded context |
| ASM01..04 | `assessment` + `reporting` |
| FIN01..04 | `finance` bounded context + `shared/money`, `shared/idempotency` |
| PRT01..04 | `portal` + `shared/outbox` |
| OPS01..04 | `operations` bounded context |
| NFR01 | `shared/rbac` + Postgres RLS |
| NFR02 | `shared/auth` |
| NFR05/06 | DB indexes + transactional allocation |
| NFR07 | `operations/attachment` |
| NFR08 | `shared/errors` + `shared/outbox` |
| NFR09 | `operations/operational_run` |
| NFR10 | OpenAPI + role docs in `/docs` |
| R01–R24 | Cross-cutting checks: `shared/rbac`, `shared/tenancy`, `shared/money`, `shared/idempotency` |
| V01–V24 | `tests/e2e/scenarios/v01_…v24_…` |