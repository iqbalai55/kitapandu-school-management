# 11: Test pyramid — fixtures, V01–V24 acceptance scenarios

**What to build:** The three-layer test suite. `tests/unit/` covers pure-Python domain logic in under 5 seconds without a DB. `tests/integration/` spins up an ephemeral Postgres 16 via `testcontainers-python`, runs `alembic upgrade head`, runs the seed, and exercises API + DB together. `tests/e2e/scenarios/` has one pytest case per V01–V24 from the data dictionary.

**Blocked by:** 00, 01, 10 (seed needed for fixtures)

**Status:** ready-for-agent

- [ ] `tests/conftest.py` provides `pg_container` (testcontainers-python `postgres:16-alpine` fixture, scope='session'), `migrated_db` (runs `alembic upgrade head` once), `seeded_db` (runs the seed once), `engine` (async SQLAlchemy engine bound to the container), `session` (per-test `AsyncSession` with RLS context), `client` (httpx `AsyncClient` against `ksma.main:app` via `ASGITransport`).
- [ ] `pytest.ini` (or `pyproject.toml [tool.pytest.ini_options]`) declares `asyncio_mode = 'auto'`, markers `unit`, `integration`, `scenario`, and `addopts = ['-ra', '--strict-markers', '--tb=short']`.
- [ ] `tests/unit/test_money.py` — `Money` arithmetic, IDR scale 0 only, negative rejected.
- [ ] `tests/unit/test_clock.py` — `Clock` protocol, freezegun-pinned `now()` and `today()`.
- [ ] `tests/unit/test_jwt.py` — JWT encode/decode round-trip, `authz_version` mismatch rejection, expired token rejection, audience mismatch rejection.
- [ ] `tests/unit/test_argon2id.py` — hash/verify round-trip, wrong-password rejection, hash never returned in any exception.
- [ ] `tests/unit/test_idempotency.py` — same-key-same-body returns cached; same-key-different-body raises `IdempotencyConflictError`; replay after grant revocation returns 403.
- [ ] `tests/unit/test_action_registry.py` — `ACTION_REGISTRY` has exactly 73 codes, every module prefix (`PLT`/`REG`/`CLS`/`SES`/`ASM`/`FIN`/`PRT`/`OPS`/`X`) has its expected count.
- [ ] `tests/integration/test_rls.py` — V01 cross-tenant isolation: AC at tenant A reads tenant B's resource → 403 empty body.
- [ ] `tests/integration/test_capacity_race.py` — V05 concurrent placements race.
- [ ] `tests/integration/test_money_invariants.py` — V13–V18.
- [ ] `tests/integration/test_idempotency.py` — V16, V22.
- [ ] `tests/integration/test_outbox_dedup.py` — V21.
- [ ] `tests/e2e/scenarios/v01_cross_tenant.py` — AC tenant A asks for `/units/{id_of_B}` → 403 empty body.
- [ ] `tests/e2e/scenarios/v02_duplicate_candidates.py` — create two participants with same name + same contact; both returned with `duplicate_suspect=true`; no auto-merge.
- [ ] `tests/e2e/scenarios/v03_invalid_dates.py` — `birth_date` tomorrow rejected; session ending before start rejected.
- [ ] `tests/e2e/scenarios/v04_exclusive_end.py` — GU relation valid_until exactly 12:00; request at 12:00 → 403.
- [ ] `tests/e2e/scenarios/v05_capacity_race.py` — capacity 10, occupancy 9, two concurrent placements → exactly one succeeds.
- [ ] `tests/e2e/scenarios/v06_overlap_rejection.py` — two enrollments same `(participant, batch)` overlapping range → 409.
- [ ] `tests/e2e/scenarios/v07_concurrent_modification.py` — two admins change same `row_version` → one commits, the other 409.
- [ ] `tests/e2e/scenarios/v08_attendance_scope.py` — attendance for wrong-batch enrollment or after service end → 409; unfilled ≠ absent.
- [ ] `tests/e2e/scenarios/v09_closure_checklist.py` — close incomplete session → 409; correction without mandate → 409.
- [ ] `tests/e2e/scenarios/v10_indicator_value.py` — numeric 101 rejected; two kinds at once rejected; zero accepted.
- [ ] `tests/e2e/scenarios/v11_evidence_scope.py` — assessment referencing another rubric's indicator rejected; draft report not visible to portal.
- [ ] `tests/e2e/scenarios/v12_report_versioning.py` — v2 publication preserves v1 snapshot; participant name change reflected in v2.
- [ ] `tests/e2e/scenarios/v13_invoice_totals.py` — line net 600k + 400k = 1M; installments sum to 1M (otherwise reject).
- [ ] `tests/e2e/scenarios/v14_recognized_vs_claimed.py` — claimed 500k, recognized 450k; difference reason recorded.
- [ ] `tests/e2e/scenarios/v15_allocation_capacity.py` — funds 1M, allocations 700k, new 350k → 409.
- [ ] `tests/e2e/scenarios/v16_concurrent_allocation.py` — installment remainder 200k, two concurrent 150k allocations → one succeeds; same idempotency key → one effect.
- [ ] `tests/e2e/scenarios/v17_credit_limit.py` — credit 350k against remainder 300k → 409; credit 200k → remainder 100k.
- [ ] `tests/e2e/scenarios/v18_reversal_consistency.py` — payment reversal without allocation reversal rejected.
- [ ] `tests/e2e/scenarios/v19_file_revoke.py` — download quarantined file rejected; download after rights revocation rejected.
- [ ] `tests/e2e/scenarios/v20_import_atomic.py` — 100-row import with one cross-tenant row → 0 rows committed.
- [ ] `tests/e2e/scenarios/v21_outbox_dedup.py` — same event consumed twice → one notification effect + one `ProcessedEvent`.
- [ ] `tests/e2e/scenarios/v22_idempotency_key.py` — same key different amount → 409; replay after revocation → 403.
- [ ] `tests/e2e/scenarios/v23_restore_isolation.py` — restore tenant A from shared backup → A's data/files/balances match; B not present; production notifications not sent.
- [ ] `tests/e2e/scenarios/v24_metric_denominator.py` — capacity zero → "not applicable" not "0"; unique participant counted once even in two classes.
- [ ] CI workflow file (e.g., `.github/workflows/test.yml` for reference; or a local `make ci` target) runs `ruff check`, `mypy src/ksma`, `pytest tests/unit`, `pytest tests/integration`, `pytest tests/e2e -m scenario` in sequence; green is the gate.