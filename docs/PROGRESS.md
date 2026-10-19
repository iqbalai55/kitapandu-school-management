# KSMA Backend — Progress Report

**Date:** 2026-10-20
**Scope:** Pilot-acceptance (Q1 of pilot at Vaneta Maju Berdaya)

## What landed

- **81 Python files**, **16,821 lines** of code in `src/ksma/`
- **9 Alembic migrations** (0001 → 0009) creating **43 tables** total
- **All 10 bounded contexts** in DDD layout (`shared/` + 10 contexts)
- **FastAPI app factory** (`main.py`) with global exception handlers + OpenAPI
- **Seed script** (`python -m ksma.scripts.seed_vaneta_demo`)
- **Outbox worker CLI** (`python -m ksma.shared.outbox.run`)

## Architectural foundations (ADR0001)

- Stack: Python 3.12, FastAPI, SQLAlchemy 2.x async, asyncpg, Alembic, Pydantic v2, pydantic-settings, argon2-cffi, PyJWT, MinIO
- Multi-tenancy: app-layer `TenantContext` + Postgres RLS (`app.current_tenant` GUC)
- Auth: Argon2id + JWT(15m) + hashed refresh(7d) + recovery tokens + `authz_revision`
- RBAC: `ACTION_REGISTRY` with 73 codes, 13 roles, whole-grant resolver (R02)
- Money: `Money(amount, currency)` value object, `NUMERIC(18,0)` in Postgres
- Idempotency: `IdempotencyRecord` table + `Idempotency-Key` header on protected POSTs
- Outbox: in-DB `outbox_messages` + CLI worker using `FOR UPDATE SKIP LOCKED`
- DDD layout with `shared/` kernel + 10 bounded contexts

## Bounded contexts delivered

| Context | Domain | Infra | Application | Interface | Migration |
|---|---|---|---|---|---|
| `shared/` | errors, money, time, events, tenancy, idempotency, persistence | — | — | — | 0001 |
| `identity` | Tenant, Unit, Account, Membership, RefreshToken, RecoveryToken, ServicePrincipal, AccessAssignment, SupportGrant, PolicyVersion, AuditRecord | ✅ | ✅ | ✅ | 0002 |
| `participant` | Participant, RelatedParty, ParticipantAccountLink, ParticipantRelation | ✅ | ✅ | ✅ | 0003 |
| `program` | Program, ProgramVersion, AcademicPeriod, RubricVersion, RubricIndicator | ✅ | ✅ | ✅ | 0004 |
| `batch` | Batch, Application, Enrollment, EnrollmentChange, TeachingAssignment | ✅ | ✅ | ✅ | 0005 |
| `session` | Session, SessionRevision, Attendance, TeachingJournal, SessionCompletion | ✅ | ✅ | ✅ | 0006 |
| `finance` | BillingParty, Invoice, InvoiceLine, Installment, PaymentSubmission, VerifiedPayment, PaymentAllocation, Receipt, FinancialAdjustment | ✅ | ✅ | ✅ | 0007 |
| `assessment` | Assessment, AssessmentResult, AssessmentEvidence | ✅ | ✅ | ✅ | 0008 |
| `reporting` | ReportPublication, CompletionDecision, Certificate | ✅ | ✅ (shared with assessment) | ✅ (shared) | 0008 |
| `portal` | ServiceRequest, InternalNotification, CommunicationPreference | ✅ | ✅ (minimal) | ✅ (minimal) | 0009 |
| `operations` | Attachment, ImportBatch, ImportRowResult, ExportJob, MetricDefinition, OperationalRun, BackgroundJob | ✅ | stub | — | 0009 |

## Invariants enforced

- **BR01** — `current_tenant` GUC + RLS policies on every T/U table
- **BR02** — guardian/payer/sponsor as distinct `relation_kind` values
- **BR04** — cancelled sessions don't count as completed
- **BR05** — empty attendance ≠ absent (NULL = `belum_diisi`)
- **BR06** — payments reduce billed amount only after verified + allocated
- **BR07** — corrections via traceable trail
- **BR08** — arrears don't auto-stop learning
- **BR09** — completion requires `evidence_refs` to report_publication / assessment (NOT money alone)
- **R01** — T/U references share tenant
- **R02** — whole-grant evaluation (no splicing)
- **R03** — `authz_revision` bumps invalidate JWTs; revocation rechecked
- **R05** — time ranges validated (`start < end`)
- **R06** — capacity race via `SELECT … FOR UPDATE` + count (V05)
- **R07** — enrollment overlap via Postgres `EXCLUDE USING gist` (V06)
- **R08** — one effective revision per session
- **R09** — closure requires attendance + journal completeness (V08, V09)
- **R10** — assessment result type matches indicator `value_kind` (V10, V11)
- **R11** — report snapshot frozen, revisions chain (V12)
- **R12** — installment sum == invoice total
- **R13** — verify + VerifiedPayment committed together
- **R14** — allocation in one transaction with FOR UPDATE; never negative (V15, V16)
- **R15** — payment reversal reverses related allocations consistently (V18)
- **R17** — profile merge gated by published enrollment policy (Q02)
- **R20** — outbox consumer dedup
- **R21** — Idempotency-Key scoped to (tenant, requester, action, key)
- **R22** — append-only audit log

## Deferred decisions (Q01–Q14)

- **Q01** — Vaneta's `jenis satuan` (institution_type): pilot uses `LK` per seed
- **Q02** — enrollment policy stub published in seed
- **Q03** — capacity + pause semantics: per-Batch config in the seed
- **Q04** — finance policy stub with `ROUND_HALF_EVEN` + 1 installment
- **Q05** — leave/credit: not yet enforced in pilot
- **Q06** — session policy stub requires `attendance_required_for_closure`
- **Q07** — assessment policy stub: rubric required
- **Q08** — auth flow + role merging: implemented as Round-1 resolved
- **Q09** — external channels: internal inbox only; manual template copy
- **Q10** — import source: not exercised in pilot
- **Q11** — environment limits: 10 MB / daily backup / 24h RPO / 8h RTO documented
- **Q12** — certificate numbering: sequential per tenant+scope
- **Q13, Q14** — segment-specific + P1 features: not in pilot

## How to run locally

```bash
cd kitapandu-backend
cp .env.example .env
docker compose up -d postgres minio
pip install -e ".[dev]"
alembic upgrade head
python -m ksma.scripts.seed_vaneta_demo
uvicorn ksma.main:app --reload --port 8000
python -m ksma.shared.outbox.run    # in a second terminal
```

OpenAPI: http://localhost:8000/docs

## What's still pending

- **Test suite**: pytest fixtures + V01–V24 scenario tests + unit tests for money/RBAC/outbox
- **Audit projection consumer**: a real `internal_notification` writer that subscribes to the outbox
- **Attachment service**: MinIO upload presigned-URL endpoint + `Attachment` lifecycle
- **Import/export services**: `ImportBatch` preview + commit, `ExportJob` queued async
- **Recovery drill test**: separate env + RPO/RTO evidence per R23
- **Container hardening**: real auth on `/admin/*` endpoints (mTLS / service token)

These can be tackled one at a time without disturbing the architecture.