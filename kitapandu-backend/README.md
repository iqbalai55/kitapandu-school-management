# KitaPandu School Management — Backend (KSMA v1)

FastAPI + PostgreSQL 16 + SQLAlchemy 2.x async backend for the KitaPandu School Management
pilot at Vaneta Maju Berdaya. Implements the full PRD v1.0 pilot-acceptance scope across
PLT, REG, CLS, SES, ASM, FIN, PRT, OPS modules. See `/docs/adr/0001-scope-pilot-acceptance.md`
and `../../GLOSSARY.md` for the architecture and canonical vocabulary.

## Layout

```
src/ksma/
  shared/                  # cross-cutting building blocks
    auth/                  # argon2, JWT, refresh, recovery, authz_revision
    money/                 # Money value object, rounding, IDR scale-0
    tenancy/               # TenantContext, RLS session helpers
    rbac/                  # ACTION_REGISTRY (73 codes), whole-grant resolver
    idempotency/           # IdempotencyRecord, header parsing
    outbox/                # OutboxEvent writer + CLI worker (SKIP LOCKED)
    errors/                # DomainError hierarchy → HTTP mapping
    events/                # DomainEvent base + dispatcher
    time/                  # Clock protocol, Asia/Jakarta helpers
    persistence/           # AsyncEngine, AsyncSession, RLS GUC set, base model
  identity/                # Tenant, Unit, Account, Membership, AccessAssignment,
                           # SupportGrant, ServicePrincipal, PolicyVersion, AuditRecord
  participant/             # Participant, RelatedParty, ParticipantAccountLink,
                           # ParticipantRelation
  program/                 # Program, ProgramVersion, AcademicPeriod, RubricVersion,
                           # RubricIndicator
  batch/                   # Batch, Application, Enrollment, EnrollmentChange,
                           # TeachingAssignment
  session/                 # Session, SessionRevision, Attendance, TeachingJournal,
                           # SessionCompletion
  assessment/              # Assessment, AssessmentResult, AssessmentEvidence
  reporting/               # ReportPublication, CompletionDecision, Certificate
  finance/                 # Invoice, InvoiceLine, Installment, PaymentSubmission,
                           # VerifiedPayment, PaymentAllocation, Receipt, FinancialAdjustment
  portal/                  # ServiceRequest, InternalNotification, CommunicationPreference
  operations/              # Attachment, ImportBatch, ImportRowResult, ExportJob,
                           # MetricDefinition, OperationalRun, BackgroundJob,
                           # ProcessedEvent, IdempotencyRecord
alembic/                   # migrations (one per logical change)
tests/                     # unit + integration + e2e (V01..V24 scenarios)
scripts/                   # CLI helpers: seed Vaneta DEMO, run outbox worker, etc.
docker-compose.yml         # dev: postgres + minio + api + worker
```

## Quick start (dev)

```bash
cd kitapandu-backend
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env
docker compose up -d postgres minio
alembic upgrade head
python -m ksma.scripts.seed_vaneta_demo
uvicorn ksma.main:app --reload --port 8000
python -m ksma.shared.outbox.run  # in a second terminal
```

OpenAPI: http://localhost:8000/docs

## Testing

Use the canonical Makefile targets from the repo root:

```bash
make test               # unit + integration
make test-acceptance    # V01..V24 PRD scenarios
make smoke              # end-to-end smoke pilot
```

You can still invoke pytest directly inside `kitapandu-backend/`:

```bash
pytest                                            # all
pytest tests/unit                                 # fast
pytest tests/integration                          # API + DB
pytest tests/e2e -m scenario -k v05               # one PRD scenario
```

## Architectural decisions

See `/docs/adr/` for the architecture decision records:

- `0001-scope-pilot-acceptance.md` — locks in the pilot-acceptance scope (all P0 modules, no P1 stubs), the Python 3.12 + FastAPI + SQLAlchemy 2 stack, app+RLS tenancy, the 73-action RBAC registry, Money/IDR, outbox+idempotency, Argon2id+JWT auth, REST + OpenAPI 3.2, and the per-BC DDD layout.
- `0002-ddd-aggregate-boundaries.md` — defines aggregate roots per bounded context, enforces cross-BC consistency via outbox + domain events + ACL ports (no direct cross-BC imports, import-linter guarded), codifies the `@audited` service-layer decorator, and fixes PascalCase singular tables / UUID PKs / `TIMESTAMPTZ` time handling.
- `0003-work-item-via-service-request.md` — reuses the `ServiceRequest` aggregate as the universal work-item primitive (with a `requester_kind=service` flag and `system_event_id` column for system-spawned items) plus an `ops_work_queue` SQL view that powers the OPS-A07 work-queue dashboard.
- `0004-action-registry-and-masking.md` — specifies the `ACTION_REGISTRY` (Python dict mirrored to an SQL table), a one-shot `can(actor, action_id, target)` whole-grant evaluator, `Scope.column_mask` + `mask_response` for column masking, and `requires_reason` flags for F08-style corrections.
- `0005-seed-and-tenancy-bootstrap.md` — defines the idempotent seed CLI that upserts two tenants (`Vaneta DEMO`, `Lembaga Uji DEMO`), units, demo users, demo participants, one published program + batch, three scheduled sessions, and minimal finance data, all under a `seed-bootstrap` `ServicePrincipal` and tracked by an `OperationalRun`.
- `0006-auth-and-jwt.md` — sets Argon2id hashing, 15-min JWT access (with `authz_version`) plus 7-day hashed refresh tokens with rotation + replay-revocation, 30-min single-use `AccountRecoveryToken`, and `authz_revision` bumps to invalidate sessions on grant/membership changes.

## Traceability

| PRD code | Module |
|---|---|
| PLT01–04 | `identity` + `shared/tenancy`, `shared/rbac` |
| REG01–04 | `participant` + `batch` |
| CLS01–04 | `program` + `batch` + `session` |
| SES01–04 | `session` |
| ASM01–04 | `assessment` + `reporting` |
| FIN01–04 | `finance` |
| PRT01–04 | `portal` |
| OPS01–04 | `operations` |
| NFR01–10 | `shared/*` + `identity` + `operations` |
| R01–R24 | `shared/*` invariants (enforced by services + DB) |
| V01–V24 | `tests/e2e/scenarios/v01_…v24_…` |