# 01: Schema baseline — RLS, exclusion constraints, missing extensions

**What to build:** Bring every T/U table under the same RLS policy, add the missing `btree_gist` exclusion constraints, and define the plpgsql helper functions that the shared kernel depends on. After this ticket lands, `ENABLE/FORCE ROW LEVEL SECURITY` covers all 55+ tables, the program-version overlap and enrollment-overlap invariants are DB-enforced, and `ksma_current_tenant()` / `ksma_bypass_rls()` exist in the database.

**Blocked by:** None (can start immediately)

**Status:** ready-for-agent

- [ ] Migration `0010_schema_baseline` (new Alembic revision depending on `0009`) that runs idempotent `CREATE EXTENSION IF NOT EXISTS btree_gist` plus the plpgsql helpers `ksma_current_tenant()` and `ksma_bypass_rls()` if they aren't already defined.
- [ ] Every T/U table (`tenant` is root; everything else is T or U) has `ENABLE ROW LEVEL SECURITY` and `FORCE ROW LEVEL SECURITY` enabled; verified by querying `pg_class` for each table name.
- [ ] Every T/U table has a policy named `<table>_tenant_isolation` whose `USING` and `WITH CHECK` clauses are `ksma_bypass_rls() OR tenant_id = ksma_current_tenant()`.
- [ ] `program_version` has an exclusion constraint on `(program_id WITH =, effective_range WITH &&)` using `btree_gist`, rejecting overlapping ranges for the same program.
- [ ] `enrollment` has an exclusion constraint on `(participant_id WITH =, batch_id WITH =, effective_range WITH &&)` using `btree_gist`, rejecting overlapping enrollments on the same `(participant, batch)` pair (R07, V06).
- [ ] All currency-bearing columns are `NUMERIC(18, 0)` with `CHECK (amount >= 0)` where negative values are forbidden.
- [ ] All status-enum columns are backed by Postgres `ENUM` types (created via `CREATE TYPE ... AS ENUM (...)`) so new values need an `ALTER TYPE ... ADD VALUE` migration; existing `text CHECK` enums are migrated.
- [ ] `down_revision = '0009'` for the new migration; `downgrade()` cleanly reverses all changes.
- [ ] Integration test that sets a tenant context, writes a row, sets a different tenant context, and asserts the second context cannot see or modify the first row (V01 cross-tenant invariant).