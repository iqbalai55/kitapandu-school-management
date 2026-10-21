# 17: Kill the Alembic schema-migration survivors — round-trip every migration in `tests/unit/test_alembic.py`

**Parent spec:** #16 (Kill the 337-survivor gap)
**What to build:** A unit test, added to `tests/unit/test_alembic.py`, that round-trips every Alembic migration's `upgrade()` → schema-check → `downgrade()` → schema-emptied against an in-memory SQLite target. Schema-check asserts the column types, nullability, RLS state, and ENUM membership that the migration is contractually obligated to leave behind. Killing the 3 surviving schema-migration sites is the end-to-end behaviour this ticket makes work — once the round-trip is in, `mutator --since-last-run` can no longer flag the schema baseline as `SURVIVED`.
**Blocked by:** None (can start immediately). The `tests/unit/test_alembic.py` skeleton already exists per ticket #14.
**Status:** ready-for-agent

- [ ] The test file `tests/unit/test_alembic.py` contains one parametrised test, indexed by migration-revision id, that runs `upgrade → inspect_base_metadata → downgrade → inspect_base_metadata_again` against `sqlite:///:memory:`.
- [ ] The 3 surviving mutations in `.metrics/mutator.txt` (`alembic/env.py:29 True → False`, `alembic/versions/2026_10_20_0240_0009_portal_operations.py:284 delete -`, `alembic/versions/2026_10_20_0240_0009_portal_operations.py:284 1 → 0`) are absent from any subsequent `.metrics/mutator.txt` run on the same branch.
- [ ] `make mutation` (per the existing Makefile target) reports 3 newly-killed sites on the Alembic module with no new survivors introduced.
- [ ] The round-trip test does NOT require Docker, `testcontainers`, or `alembic` on PATH — it parses the migration files directly via Alembic's scripting API only.
- [ ] No production code under `kitapandu-backend/alembic/` is modified (the spec forbids source changes in this wave).
