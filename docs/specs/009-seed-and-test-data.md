# Spec 009 — Seed Data, Test Pyramid, and Pilot Acceptance

> Cross-cutting concern: how a fresh checkout of `kitapandu-backend/` becomes a
> demoable system, and how the V01–V24 acceptance scenarios from the data
> dictionary become a runnable test suite.

## Problem Statement

The pilot iteration is meant to demonstrate the system end-to-end at Vaneta
Maju Berdaya. To do that:

1. Developers need a single command that boots a runnable stack with two
   seeded tenants (Vaneta DEMO + Lembaga Uji DEMO) so cross-tenant isolation
   is observable in dev.
2. The integration tests need a deterministic fixture set (V01 needs two
   tenants; V05 needs a batch with capacity 10 and 9 existing enrollments;
   V13 needs an invoice with two lines summing to 1,000,000).
3. The V01–V24 acceptance scenarios from the data dictionary must each be a
   pytest case that passes against the seeded state.

## Solution

Three deliverables:

1. **`ksma.scripts.seed_vaneta_demo`** — an idempotent CLI that creates the
   two demo tenants, the demo participants, the demo program + batch + sessions,
   and one happy-path money chain. Records an `OperationalRun` row for the
   seed.
2. **`tests/conftest.py`** — fixtures that build an ephemeral Postgres 16
   container via `testcontainers-python`, run `alembic upgrade head`, run the
   seed, and provide a `client` (httpx.AsyncClient against the FastAPI app)
   and a `session` (async SQLAlchemy session).
3. **`tests/e2e/scenarios/v01_cross_tenant.py` … `v24_metric_denominator.py`** —
   24 pytest scenarios, one per `V01–V24` from the data dictionary.

## User Stories

### Seed
1. As a **developer**, I want `make seed` (or `python -m ksma.scripts.seed_vaneta_demo`)
   to populate the dev DB so that I can demo the system without writing fixtures.
2. As a **developer**, I want the seed to be idempotent — re-running it should
   not create duplicates or fail.
3. As a **developer**, I want the seed to write an `OperationalRun` row so that
   the audit trail shows when demo data was created.

### Test pyramid
4. As a **developer**, I want `pytest tests/unit` to run without a DB, in under
   5 seconds, so I can iterate on pure-Python domain code.
5. As a **developer**, I want `pytest tests/integration` to spin up an
   ephemeral Postgres via `testcontainers-python`, so that RLS and FOR UPDATE
   semantics are tested against real Postgres.
6. As a **developer**, I want `pytest tests/e2e -m scenario -k v05` to run just
   the V05 capacity race test, so I can debug it.
7. As a **QA engineer**, I want every V01–V24 scenario to be one pytest case,
   so that pilot UAT traceability is direct.

### Pilot acceptance
8. As a **product manager**, I want the pilot acceptance checklist (PRD §20)
   to map to a test pass, so that "all P0 are accepted" is a green test run.
9. As a **demo presenter**, I want a `make demo` script that brings up the
   stack, seeds, and prints the demo credentials, so that demos run smoothly.

## Implementation Decisions

### Seed content
- Tenant `Vaneta DEMO` (display_name "Vaneta Maju Berdaya (DEMO)"),
  default timezone `Asia/Jakarta`, service_status `active`.
- Tenant `Lembaga Uji DEMO` (display_name "Lembaga Uji Demo (PENGGANTI)").
- One `EducationUnit` per tenant (`unit_code='VANETA-UNIT-A'`,
  `institution_type='lk_lkp'`, `education_levels=['TK']`, `timezone='Asia/Jakarta'`).
- Users: `op@kitapandu.id` (OP), `am@vaneta.id` (AM),
  `ow@vaneta.id` (OW), `ac@vaneta.id` (AC), `fi@vaneta.id` (FI),
  `in@vaneta.id` (IN), `as@vaneta.id` (AS), `ap@vaneta.id` (AP),
  `au@vaneta.id` (AU), `st@vaneta.id` (ST), `gu@vaneta.id` (GU),
  `py@vaneta.id` (PY). All passwords `Demo!2345`. Same set on the second tenant
  with `@lembaga.id` suffix.
- Five demo participants in Vaneta: `Sari Wulandari` (P-0001),
  `Budi Santoso` (P-0002), `Dewi Lestari` (P-0003), `Andi Pratama` (P-0004),
  `Rina Kusuma` (P-0005). A second `Sari` with a different `participant_no`
  (P-0006) to exercise V02 duplicate-candidate detection.
- One `Program` (`EN-A1`, `service_group='language'`) + one published
  `ProgramVersion` (v1, `learning_objectives='Can introduce self...'`).
- One `Batch` (`EN-A1-Okt2026`, `capacity=10`, `delivery_mode='onsite'`,
  `status='open'`).
- Three `Session` rows scheduled across the next 14 days (one per week).
- Two `Invoice` rows: `INV-2026-0001` for P-0001 with one installment of
  Rp 1,000,000; `INV-2026-0002` for P-0002 with two installments (Rp 600,000
  + Rp 400,000). Both in `status='issued'`.
- One `PaymentSubmission` → one `VerifiedPayment` (Rp 1,000,000) →
  one `PaymentAllocation` against INV-2026-0001.
- One `Attendance` row for P-0001 in the first scheduled session
  (`attendance_code='hadir'`) — for V08 sanity.
- All writes use `created_by = 'service:seed-bootstrap'` and `created_by_actor = 'seed'`.
- An `OperationalRun(operation_kind='seed', environment='development',
  execution_subject_id=<seed-bootstrap-service-principal>)` row records the seed.

### Test pyramid
- `tests/unit/` — pure-Python domain tests. Cover `Money` arithmetic,
  `Scale.validate_value`, action-registry lookup, JWT encode/decode,
  Argon2id hash/verify, idempotency-key parser, outbox payload serializer.
- `tests/integration/` — `testcontainers-python` ephemeral Postgres 16,
  `alembic upgrade head` against it, seed runs. `httpx.AsyncClient`
  against the FastAPI app via `ASGITransport`. Covers R02, R06, R07, R12,
  R14, R15, R18, R21 and end-to-end BC flows.
- `tests/e2e/scenarios/` — V01–V24. Each file has one pytest scenario
  decorated with `@pytest.mark.scenario("v01")` and a docstring that quotes
  the data dictionary scenario verbatim.

### Pilot acceptance checklist mapping (PRD §20)
- Money/access/activity/completion policies → covered by `PolicyVersion` rows in seed.
- Initial data reconciled → covered by seed `OperationalRun`.
- All P0 and NFR UAT scenarios pass → `tests/e2e/scenarios/v01_v24.py`.
- Admin/finance/instructor/participant/guardian users execute main flow →
  smoke script in `scripts/smoke_pilot.py`.
- Invoice/payment/allocation/reversal reconciled → smoke script asserts
  paid_status == 'paid', arrears == 0.
- Backup restored in separate environment → covered by R23, V23 acceptance test.
- No critical issue → captured as regression test before each release.

### Seams
- The seed CLI uses the same `require_action` resolver, just with a `ServicePrincipal`
  grant that bypasses normal checks for the bootstrap identities.
- Test fixtures use a separate `ServicePrincipal` grant set to write seed
  rows in test tenants.

## Testing Decisions

What makes a good test:
- A V01–V24 acceptance test asserts **the scenario's expected outcome** as
  documented in the data dictionary, not the implementation.
- A seed test asserts that running `seed.run` twice produces the same DB state
  (no duplicates).
- An integration test asserts **DB state** after the API call, not log output.
- A smoke test runs through: login → place participant → issue invoice →
  submit payment → verify → allocate → confirm receipt is downloadable.

## Out of Scope

- Continuous-load test (PRD NFR05 proposes 20 concurrent users, 500 participants,
  10,000 attendance records; that's a k6 / Locust script, separate from this spec).
- Backup infrastructure (only the evidence-recording is in scope per spec 008).
- Pilot UAT sign-off process (handled by the product manager, not in code).

## Further Notes

- The existing `src/ksma/scripts/seed_vaneta_demo.py` is a 6-line placeholder.
  This spec describes the real seed.
- No tests exist yet (`tests/{unit,integration,e2e/scenarios}` are all empty).
  This spec is the contract for what to build first.
- Remaining work:
  1. Replace the placeholder seed with the full implementation above.
  2. Add `testcontainers-python` to dev dependencies (already in pyproject).
  3. Write the V01–V24 scenario files.
  4. Add `make seed`, `make test`, `make test-acceptance`, `make smoke`
     targets to the Makefile (or `taskfile.yml`).
  5. Add a `scripts/smoke_pilot.py` that exercises the happy-path chain.