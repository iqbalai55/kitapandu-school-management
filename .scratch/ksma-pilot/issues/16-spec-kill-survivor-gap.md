# 16: Close the 337-survivor gap — measurable kill rate ≥ 95 % on the live baseline

> **Status:** `ready-for-agent`
> **Supersedes / extends:** #14 (P5.1 "kill all surviving mutants" wave — 977 unit tests landed but never measured on the live baseline)
> **Hardening follow-up:** #15 (P5.1.4 — removes dead runners and clears lint debt from the #14 wave)
> **Recorded in:** `.metrics/harness-report.md` Recommendation #2 (2026-10-20 commit)
> **Pinned measurement:** branch `fix/mutator-baseline-2026-10-20` @ `main` (sha `5129917`).
>
> **Blocked by:** none. The mutator baseline is now green (see `make harness-mutation` documented in Caveats) so every number in this spec is live, not historical.

---

## Problem Statement

The KSMA pilot cannot close its acceptance gate while the change-resilience verdict in `.metrics/harness-report.md` reads `FAIL — 374/711 mutants killed (52.6 %); 337 survivors`. From the perspective of a pilot audit engineer signing off the harness, **every surviving mutant is a domain rule that the unit suite cannot detect when broken**. Operators cannot certify the pilot when ~48 % of operator flips inside financial, batch-lifecycle, identity, observability, and seed-code functions slip past the test pyramid:

- `finance/application/__init__.py` (57 survivors, including V13-V18 invoice + installment allocation paths)
- `operations/application/__init__.py` (47 survivors, including the V20 atomic-commit guarantee on `commit_import`)
- `scripts/seed_vaneta_demo/__init__.py` (34 survivors — the seed path that the smoke pilot depends on)
- `identity/application/__init__.py` (29 survivors, including password hashing and grant revocation)
- `scripts/smoke_pilot/__init__.py` (22 survivors — the end-to-end demo driver)
- `batch/application/__init__.py` (16 survivors, including the V05 capacity-limit and V06 exclusion-constraint guards)
- `alembic/env.py:29` and `alembic/versions/2026_10_20_0240_0009_portal_operations.py:284` (3 survivors in schema migration — the structural baseline every other migration inherits from)
- … and 11 further modules producing 27+ more survivors each.

Ticket #14 already shipped 977 unit tests across 30 modules on `integration/kill-mutants`. The wave moved the kill rate from 59/712 (8.3 %) up to a live measurement of 374/711 (52.6 %), but the survivor list above was never enumerated against a working baseline. That enumeration now exists in `.metrics/mutator.txt` (337 lines beginning with `SURVIVED`). Closing the remaining gap is a discrete, enumerable body of work — there is no design ambiguity left, only the question of which assertions to pin.

## Solution

A focused second wave that adds **one domain-shaped assertion per surviving mutation** across the 35 files that contribute survivors, drives the live kill rate to ≥ 95 %, and locks the result in via a CI gate. Concretely:

1. **Reuse the existing test pyramid** that #14 built. **All new tests land in `tests/unit/`**, one test file per source module, mirroring the canonical seam declared in `tests/unit/test_rbac_action_registry.py` (top-of-file docstring: module name, public surface, behaviour asserted, behaviour not asserted).
2. **Pin every existing assertion to a domain invariant, not "no exception".** A test that asserts only `result is not None` is not a good test for change-resilience work; a test that asserts `result.value == 5 and result.kind == "numeric"` is.
3. **For each surviving mutation, write one failing test first, then fix the assertion's substance rather than the production code.** No source-code change is permitted in this wave (per ticket #14's "no source change" rule); the only acceptable production-side edit is the deletion of dead branches, and only when the dead branch is documented in an ADR.
4. **Run the gate after each new test file** using the exact command form that produced the 52.6 % baseline:

   ```bash
   mutator --root "$(pwd)" \
     --since-last-run \
     --test-command "bash -c 'cd kitapandu-backend && python3 -m pytest tests/unit -q --no-header --no-cov'" \
     --max-workers 2 --no-coverage
   ```

   `--max-workers 2` is the user's memory constraint (matches ticket #14 and the prior assessment's setting). The 711-site baseline run took 51 min 12 s wall at this setting; a 5-20-site delta on a single file lands in 30-90 s.
5. **Wire a CI gate** that runs `mutator --since-last-run --max-workers 2 --no-coverage --test-command "...tests/unit..."` on every PR and fails the build if any previously-killed site reverts to surviving.

The result, when measured against the same harness that produced the 52.6 % baseline, must be **≥ 95 % of all 711 sites killed (i.e. ≤ 35 survivors)** with each residual survivor explicitly enumerated under `Surviving mutants` in `.metrics/harness-report.md`. The integration-side surface (currently hidden by the `--test-command "tests/unit"` filter) is documented under #15's follow-up work and remains a separate ticket.

## User Stories

1. As a **pilot audit engineer**, I want every line beginning with `SURVIVED` in `.metrics/mutator.txt` to disappear (or be annotated with `# pragma: no mutate` + a justification naming the operator being suppressed and the test fixture that justifies the suppression), so that no operator flip in any application-layer function slips past the gate.
2. As a **pilot audit engineer**, I want the 337 survivor lines to be addressable *by file* (today the hot-file list is `finance/application 57`, `operations/application 47`, `seed_vaneta_demo 34`, `identity/application 29`, `smoke_pilot 22`, `batch/application 16`, `operations/infrastructure/repositories 13`, `finance/infrastructure/repositories 12`, `assessment/application 11`, `participant/interface 8`, `observability 8`, `batch/interface 8`, `shared/auth/dependencies 7`, `operations/interface 7`, `program/application 6`), so that I can scope the work into 35 file-local commits.
3. As a **pilot audit engineer**, I want `finance.application.{compute_invoice_projection, allocate_funds, invoice_credit, verify_payment, void_invoice}` (the 5 finance-application functions on the top-22 CRAP table plus the 57 survivor lines in `finance/application/__init__.py`) to have parametrised tests asserting each invariant from `docs/specs/006-finance.md` (V13-V18: line totals, installment sum, allocation capacity, reversal pairing, recognised-vs-claimed delta), so that any `<` → `<=`, `>` → `>=`, `+` → `-`, or `or` → `and` flip in those functions fails a test.
4. As a **pilot audit engineer**, I want `operations.application.{preview_import, commit_import, scan_attachment, read_attachment_bytes, compute_metric, define_metric, start_operational_run, finish_operational_run}` (8 operations-application functions, the second-hottest file) to have tests asserting the V20 atomic-commit guarantee (100-row import with one bad row → 0 rows committed), the cross-tenant rejection, and the metric-dispatch tables, so that `* → /`, `0 → 1`, `+ → -`, and `or → and` flips at the 47 survivor sites all fail a test.
5. As a **pilot audit engineer**, I want the entire `ksma.batch.application` slice (16 survivors on `place_enrollment`, `transfer_enrollment`, `decide_application`, `change_batch_capacity`, `assign_teacher`, `open_batch`, `pause_enrollment`, `resume_enrollment`, `submit_application`, `withdraw_enrollment`) to have parametrised tests for the V05 capacity-limit enforcement, the V06 overlap-by-exclusion-constraint rejection, and the V07 concurrent-update serialization, so that every `<=` → `<`, `>=` → `>`, `1` → `0`, and `0` → `1` flip in those 16 lines is killed.
6. As a **pilot audit engineer**, I want `ksma.identity.application` (29 survivors) to have tests for every `==` → `!=` flip (lines 102, 188, 207, 252, 258, 261, 277, 285, 323, 334, 338, 353, 363, 367, 431, 432, 443, 444, 514, 540, 560, 561, 592, 599, 622) so that the password-hash comparison and grant-revocation boundary from ADR-0006 (Q08) is genuinely verified.
7. As a **pilot audit engineer**, I want the Alembic migration env (`alembic/env.py:29 True → False`, plus the 2 survivors in `2026_10_20_0240_0009_portal_operations.py:284`) to be covered by a round-trip test that calls `upgrade → schema check → downgrade → schema check` against an in-memory SQLite target (no Docker required), so that the structural baseline every migration inherits from is guarded.
8. As a **pilot audit engineer**, I want every existing test in `tests/unit/` that currently asserts only `result is not None` to be extended with a positive assertion on the resulting type / value / state (the `test_admin_token`, `test_auth`, `test_idempotency`, `test_money`, `test_clock`, `test_observability`, `test_outbox`, `test_tenancy`, `test_rbac_action_registry` skeletons), so that a `# pragma: no mutate` annotation is never an option for the "easy" hits.
9. As a **pilot audit engineer**, I want the seam in each new test file to mirror `tests/unit/test_rbac_action_registry.py` lines 1-14 — a top-of-file docstring naming the module, the public surface, the behaviour asserted, and the behaviour not asserted — so that the test layout remains uniform across the 35 files touched.
10. As a **pilot audit engineer**, I want `ksma.shared.auth.dependencies.{tenant_context_dep, load_active_grants, require_action}` (7 survivors) to have a parametrised test over every JWT shape (missing / expired / wrong tenant / `authz_version` mismatch) so that the FastAPI dependency from `docs/specs/000-umbrella-platform.md` is guarded against every `==` → `!=` flip the audit enumerates.
11. As a **pilot audit engineer**, I want every Alembic migration in `alembic/versions/*.py` to have at least one round-trip test (matching the migration-test pattern in `tests/unit/test_alembic.py`) so that the schema-only invariants — column types, nullability, RLS existence, ENUM membership — are caught by an `uncovered` mutation becoming `killed`.
12. As a **pilot audit engineer**, I want `tests/unit/test_idempotency.py` to be extended with a test asserting that a same-key/different-body replay raises the documented `IdempotencyConflictError` *and* that a replay after grant revocation returns 403 (the existing V16 + V22 contract), so that the `> → >=` and `or → and` flips in `ksma.shared.idempotency.check_or_register` are killed.
13. As a **pilot audit engineer**, I want `tests/unit/test_money.py` extended with a test asserting that adding two `Money` values in different currencies raises `ValidationError` rather than silently coercing — even when the amounts happen to match — so that an `==` → `!=` flip in `ksma.shared.money.Money.__eq__` is caught.
14. As a **pilot audit engineer**, I want `tests/unit/test_clock.py` extended with a `freezegun`-driven test asserting `Clock.now()` and `Clock.today()` are stable (same instant, same date, no timezone drift) so that the `or` → `and` mutations in `ksma.shared.time.{local_date, combine_to_instant}` cannot pass the suite.
15. As a **pilot audit engineer**, I want `tests/unit/test_observability.py` extended with a test asserting that `_NoOpSpan` / `_NoOpTracer` produce identical records whether `now()` is `True` or `False`, so that the `True → False` flips in `ksma.observability.middleware.CorrelationIdMiddleware.dispatch` (line 48) and `setup_tracing` (lines 188, 207) are caught.
16. As a **pilot audit engineer**, I want `tests/unit/test_outbox.py` extended with a test asserting that an outbox event is **not** enqueued when the surrounding `session_scope()` context exits without committing (the `enqueue` precondition from `docs/specs/000-umbrella-platform.md`), so that the `True → False` and `0 → 1` flips in `run_forever` (lines 178, 181) are killed.
17. As a **pilot audit engineer**, I want `tests/unit/test_tenancy.py` extended with a parametrised test over every member of the 13-role enum asserting `TenantContext.is_service`, `is_support`, `is_account`, `has_role` return the documented `True` / `False`, so that the `True → False` and `False → True` flips in `ksma.shared.tenancy.TenantContext.has_role` and the predicate methods are caught.
18. As a **pilot audit engineer**, I want `tests/unit/test_rbac_action_registry.py` extended so the surviving `from_json` and `evaluate` mutations at lines 429, 453-457, 519, 521, 525, 534, 536 in `ksma.shared.rbac` are killed by parametrised cases over every `ScopeKind` enum member — this is the only file in the umbrella that already has a "complete" test (77 % coverage) and so is the highest-leverage single-file edit in the wave.
19. As a **pilot audit engineer**, I want every migration round-trip in `tests/unit/test_alembic.py` extended to assert that the schema downgrade leaves the database in a state where re-applying `upgrade head` produces no-ops (idempotency), so that the 3 surviving mutations in the alembic revisions are killed.
20. As a **pilot audit engineer**, I want `tests/unit/test_errors.py` extended with a test asserting the HTTP-status mapping in `ksma.main` global handler for every code in `INVALID_VALUE`, `REQUIRED_FIELD`, `REFERENCE_SCOPE_MISMATCH`, `STATE_CONFLICT`, `VERSION_CONFLICT`, `CAPACITY_EXCEEDED`, `ACCESS_DENIED`, `IDEMPOTENCY_CONFLICT`, `POLICY_NOT_CONFIGURED`, and that the body never leaks an object payload when the code is `ACCESS_DENIED` (NFR from `docs/specs/000-umbrella-platform.md`), so that any operator flip inside `ksma.shared.errors.{DomainError, ErrorEnvelope}` is killed.
21. As a **pilot audit engineer**, I want every site this spec explicitly covers to appear in `.metrics/mutator.txt` with `Killed = Sites` and `Survived = 0`, so that an agent can grep for the gate value and verify the spec closed.
22. As a **pilot audit engineer**, I want the residual survivors (the ≤ 35 that survive after the wave) to be explicitly enumerated in `.metrics/harness-report.md` under `Surviving mutants`, so that the parent spec #35 can be closed against a measured `FAIL` rather than an inherited 8.3 %.
23. As a **pilot audit engineer**, I want `.metrics/mutate/*.edn` to *not* be deleted between runs — the `--since-last-run` snapshot is the only thing keeping each cycle to 5-15 minutes per touched file.
24. As a **pilot audit engineer**, I want the mutator invocation to be exposed as `make harness-mutation` so that the team's CI workflow uses the same command (incl. `--max-workers 2`, `--no-coverage`, `--test-command "...tests/unit..."`) and subsequent audits cannot regress to a `TESTS BROKEN` baseline failure.
25. As a **pilot audit engineer**, I want `mutator` to run on every PR via a GitHub Actions workflow (or `make` target added to `make ci`) that fails the build if any previously-killed site goes back to surviving, so the trend is durable across future work.

## Implementation Decisions

- **Seam: existing unit-test directory.** All new tests land under `kitapandu-backend/tests/unit/`. The pattern is one test file per source module, mirroring the source layout (`src/ksma/finance/application/__init__.py` → `tests/unit/test_finance_application.py`). The test file declares its seam in a top-of-file docstring matching `tests/unit/test_rbac_action_registry.py` lines 1-14. This seam was established by #14; no new seam is introduced.
- **No new testing framework, no new fixtures.** Use `pytest` + `pytest.mark.parametrize` only. Where a function-under-test requires one, build a tiny in-memory fake `AsyncSession` / repository stub inside the test file, matching the pattern already used for `Clock` (via `freezegun`) and `IdempotencyStore` (via in-memory dict).
- **No source-code change in this wave.** Per ticket #14's "no source change" rule. If a mutation survives because the production code contains a genuinely unreachable branch, the correct response is to delete the branch in a separate ticket that ships the supporting ADR — do not delete here.
- **Assertions must be domain-shaped.** Where a function returns a Pydantic model, assert on the model's typed fields (`assert result.status == AssessmentStatus.evaluated`); where a function raises, assert the exception class and the `code` attribute; where a function mutates a repository, assert the fake's post-condition. JSON-style string assertions (`result["status"] == "evaluated"`) are not acceptable on this seam.
- **Parametrise over boundary tables.** Where a function dispatches on a small enum (attendance codes, lifecycle states, role codes, scope kinds), parametrise the table so that one mutation per failing operator per row becomes a discrete, named test case (e.g. `test_set_result_two_value_kinds_rejected_with_INDICATOR_SCOPE_MISMATCH`).
- **Property-based tests where invariant-heavy.** `Money` additivity, `IdempotencyStore` lookup/reserve/conflict, `Clock` determinism — use `pytest.mark.parametrize` with explicit boundary inputs in preference to adding `hypothesis` as a new dependency (the existing `pyproject.toml` does not list it, and #15 / #14 both excluded new deps).
- **Mutation-test command.** Verifying any change in this spec uses the exact command:

  ```bash
  mutator --root "$(pwd)" \
    --since-last-run \
    --test-command "bash -c 'cd kitapandu-backend && python3 -m pytest tests/unit -q --no-header --no-cov'" \
    --max-workers 2 --no-coverage
  ```

  `--max-workers 2` is the user's memory constraint (matches the #14 ticket's recommendation and the prior assessment's setting). The full baseline takes 51 min 12 s wall; `--since-last-run` keeps incremental cycles to 5-15 minutes per touched file.
- **Coverage gate.** `pytest --cov=ksma --cov-report=term-missing` must show no function in the top-22 CRAP table below 80 % line coverage once this spec is closed (the top-7 functions at 0 % coverage are the priority user stories #3-#6 above).
- **CI integration.** Add `make harness-mutation` to the root `Makefile` matching the command above, and add a `make mutation` step into the `make ci` target that ticket #13 introduces.
- **Alembic migrations inside unit tests.** Round-trip upgrade/downgrade tests live in `tests/unit/test_alembic.py` rather than as integration tests, because the schema-only checks (column types, nullability, RLS, ENUM membership) can be exercised without a live database by parsing the migration files directly.
- **Suppress only with documented justification.** Where a survivor is genuinely unreachable, annotate with `# pragma: no mutate` plus a comment naming the operator being suppressed and the test fixture or ADR that justifies the suppression. Do not use bare suppressions.
- **Harness-report refresh.** When the wave is closed, re-run the harness (`make harness-report` or the chain `crapper --root .` then `dryer --root .` then `mutator --root . ...`) and update `.metrics/harness-report.md`. The `Change resilience:` verdict must shift from `FAIL (374/711, 52.6 %)` to either `PASS` or a narrower `FAIL` with the residual survivors explicitly enumerated.

## Testing Decisions

- **What makes a good test (for this spec):** the test fails when any single `assert`-grade operator in the function-under-test is flipped. A test that asserts only `result is not None` is *not* good enough; a test that asserts `result.value == 5 and result.kind == "numeric"` is. This sentence is the contract.
- **The seam is the public Python surface** of the function-under-test. Tests assert on inputs, outputs, and observable side effects on the fake repository — not on internal helpers. Do not patch private attributes; do not reach into `_helper` functions.
- **Modules to be tested (35 files, mirroring the survivor distribution):**
  - `kitapandu-backend/src/ksma/finance/application/__init__.py` — 57 survivors
  - `kitapandu-backend/src/ksma/operations/application/__init__.py` — 47 survivors
  - `kitapandu-backend/src/ksma/scripts/seed_vaneta_demo/__init__.py` — 34 survivors
  - `kitapandu-backend/src/ksma/identity/application/__init__.py` — 29 survivors
  - `kitapandu-backend/src/ksma/scripts/smoke_pilot/__init__.py` — 22 survivors
  - `kitapandu-backend/src/ksma/batch/application/__init__.py` — 16 survivors
  - `kitapandu-backend/src/ksma/operations/infrastructure/repositories.py` — 13 survivors
  - `kitapandu-backend/src/ksma/finance/infrastructure/repositories.py` — 12 survivors
  - `kitapandu-backend/src/ksma/assessment/application/__init__.py` — 11 survivors
  - `kitapandu-backend/src/ksma/participant/interface/__init__.py` — 8 survivors
  - `kitapandu-backend/src/ksma/observability/__init__.py` — 8 survivors
  - `kitapandu-backend/src/ksma/batch/interface/__init__.py` — 8 survivors
  - `kitapandu-backend/src/ksma/shared/auth/dependencies.py` — 7 survivors
  - `kitapandu-backend/src/ksma/operations/interface/__init__.py` — 7 survivors
  - `kitapandu-backend/src/ksma/program/application/__init__.py` — 6 survivors
  - Plus ~20 further modules contributing 1-6 survivors each (`audit`, `auth/admin_token`, `auth/admin_token_middleware`, `auth/jwt`, `batching/operations`, `participant/application`, `participant/infrastructure/repositories`, `participant/domain`, `program/interface`, `reporting/infrastructure/repositories`, `seed`, `session/application`, `session/interface`, `shared/events`, `shared/idempotency`, `shared/money`, `shared/outbox`, `shared/persistence`, `shared/rbac`, `shared/tenancy`).
- **Prior art for the tests (look here first):**
  - `tests/unit/test_rbac_action_registry.py` — the canonical seam-declared test file (lines 1-14 docstring; class-per-scenario organisation; parametrised tables).
  - `tests/unit/test_idempotency.py` — uses an in-memory fake store (`IdempotencyStore` instantiated in-memory; no DB).
  - `tests/unit/test_money.py` — pure-Python value-object assertions; parametrise over boundary inputs.
  - `tests/unit/test_alembic.py` — round-trip upgrade/downgrade using migration files directly (no DB).
- **Mutation-test command** is documented above under Implementation Decisions.
- **Acceptance metric:** every function this spec explicitly covers appears in `.metrics/mutator.txt` with `Killed = Sites` and `Survived = 0`; the harness `Change resilience:` verdict shifts from `FAIL (374/711, 52.6 %)` to `PASS (≥ 95 %)` (or to a narrower `FAIL` with the residual ≤ 35 survivors explicitly enumerated).

## Out of Scope

- **Integration tests.** Running the migration up/down against a real Postgres, exercising RLS policies, or running the V01-V24 acceptance scenarios under `tests/e2e/scenarios/` — these require `testcontainers-python` + Docker + the `alembic` CLI on `PATH`, none of which are available in this runner. Tracked under ticket #15's follow-up wave.
- **Refactoring production code.** Splitting `preview_import` (CC 23) into smaller pure functions, extracting the repository-clone hotspot in `assessment/infrastructure/repositories.py`, etc. — these are separate tickets and need their own ADRs.
- **Adding `hypothesis` or other new dependencies** that are not already declared in `kitapandu-backend/pyproject.toml`.
- **Removing dead code.** If a mutation survives because the production code has a genuinely unreachable branch, the correct response is to delete the branch in a separate ticket that ships the ADR; do not delete here.
- **Restoring the integration baseline.** Pointing `tests/integration/conftest.py:183` at `python3 -m alembic` (or adding the `alembic` script to a venv that is on PATH) so the 44 fixture-time errors clear — this is a follow-up ticket under #15 and is *not* a prerequisite for this spec, because the `--test-command "...tests/unit..."` filter excludes `tests/integration` and produces a measurable kill-rate regardless.
- **Coverage of the alembic `downgrade()` paths** beyond the round-trip shape (column types, nullability, RLS existence, ENUM membership). Deep correctness of `downgrade()` is migration-specific and lives with the migration author.
- **Rewriting existing tests wholesale.** This spec extends `#14`-landed tests with positive assertions; it does not replace them. Where the seam is already declared correctly, the work is additive.

## Further Notes

- **Live measurement.** The 374 / 711 (52.6 %) number is the live measurement from `fix/mutator-baseline-2026-10-20` @ `main` `5129917`, captured in `.metrics/harness-report.md` and `.metrics/mutator.txt`. Every survivor line is enumerated; total file count is in `.scratch/ksma-pilot/issues/16-spec-kill-survivor-gap.md` (this spec).
- **Reference to ticket #14.** This spec is the actionable follow-up to ticket #14. #14 declared the no-source-change constraint, established the seam, and shipped 977 unit tests. This spec drives the wave to closure against a measurable baseline. The 977 tests shipped in #14 will individually be revisited and tightened where they currently assert only "no exception".
- **Reference to ticket #15.** #15 ("P5.1.4 — pilot hardening") cleans up tech debt from #14's wave (3 P42 tests skipped, 351 ruff errors bypassed, dead mutation-harness runner). #15 must land before this spec's CI gate is enforced — otherwise the bypasses mask real lint regressions in the new test files.
- **Reference to ticket #13.** #13 ("P5.4 — Makefile + CI wiring") introduces the `make ci` target that this spec's CI hook rides on. Confirm #13 has landed or is in flight before adding the `make mutation` step.
- **Acceptance is measurable, not aspirational.** The spec closes when `mutator --root . --since-last-run --max-workers 2 --no-coverage --test-command "bash -c 'cd kitapandu-backend && python3 -m pytest tests/unit -q --no-header --no-cov'"` reports Killed / (Killed + Survived) ≥ 0.95 with the 337-survivor enumeration reduced to ≤ 35 lines (each annotated or implicitly killed). The full re-run is ~51 min wall at this setting, which is acceptable on the CI runner but not on a developer laptop — the gate is CI-only, run on merge.
- **Memory constraint is encoded.** `--max-workers 2` is the user's memory constraint and is recorded in this spec verbatim. Do not raise it on the agent side without the user's explicit approval.
- **Harness data flow.** `make harness-report` → `crapper --root .` (writes `.metrics/crapper.txt` + `.metrics/crap.edn`), then `dryer --root .` (writes `.metrics/dryer.txt` + `.metrics/dry.edn`), then `mutator --root . --since-last-run ... --max-workers 2 --no-coverage --test-command "...tests/unit..."` (writes `.metrics/mutator.txt` + 100+ `.metrics/mutate/*.edn`). Coverage LCOV is re-emitted at `target/coverage/python/kitapandu-backend/lcov.info`.
- **Status convention.** When this spec closes, the verdict in `.metrics/harness-report.md` reads `PASS — K/N mutants killed (P%)` (preferred outcome), *or* `FAIL — K/N mutants killed (P%); <S> survivors`, where `<S> ≤ 35` and every survivor line is enumerated under `Surviving mutants`. The residuals are tracked under the next wave.

---

*Status: `ready-for-agent`. Acceptance gate: mutator kill rate ≥ 95 % on the live 711-site baseline at `--max-workers 2 --no-coverage` with the documented `--test-command`. Wall-budget for the full baseline re-run: ~51 min; per-touched-file delta: 5-15 min.*
