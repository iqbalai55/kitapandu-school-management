# ADR-0005: Seed Strategy and Tenancy Bootstrap

- **Status**: Accepted
- **Date**: 2026-10-20
- **Supersedes**: none
- **See also**: ADR-0002 (test strategy), ADR-0006 (auth, TBD).

## Context

V01 (cross-tenant reference rejected) and the dashboard smoke tests require at least two
tenants. PRD pilot scope calls out Vaneta Maju Berdaya as the first pilot and a second test
customer for separation testing.

## Decision

`python -m ksma.seed.run` is an idempotent CLI that creates or upserts:

- Tenant `Vaneta DEMO` (display name "Vaneta Maju Berdaya (DEMO)"), service status `active`,
  default timezone `Asia/Jakarta`.
- Tenant `Lembaga Uji DEMO` (display name "Lembaga Uji Demo (PENGGANTI)") for isolation tests.
- One unit each under each tenant; per-user grants.
- Users (`ac`, `fi`, `in`, `ow`) for each tenant.
- Demo participants with realistic Indonesian names, two participants with name "Sari" but
  different `participant_no` (V02 expected behaviour: duplicate candidates, not auto-merge).
- One program + one published ProgramVersion + one Batch per Vaneta unit.
- Three sessions scheduled across the next 14 days for the demo batch.
- Two invoices with one installment each, one verified payment, one allocation for the Vaneta
  tenant (V13–V17 happy-path smoke).
- Audit + outbox events for every write.

All entities carry `created_by = ServicePrincipal('seed-bootstrap')` and the entire seed run is
recorded in an `OperationalRun(operation_kind='seed', environment='development')` row.

The seed is **safe to re-run**: every entity has a stable `display_name` or `code` and the seeder
checks for existence before insert.

## Consequences

- `make seed` produces a demoable system.
- Integration tests can rely on `vaneta_demo` / `lembaga_uji_demo` tenants being present.
- `OperationalRun` evidence for seed gives a clean audit lineage.
- Demo data is fictional; no real NIK, no real transfer references.

## Deferred

- Q01 (official unit identity), Q03 (first programs/capacity) are NOT silently applied. The seed
  uses DEMO identifiers explicitly; production seed awaits the resolved Q-decisions.