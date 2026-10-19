# 04: Program + class slice — program version, batch, teaching assignment

**What to build:** A complete program-to-batch flow: AC creates a program, publishes a new version, creates an academic period, opens a batch with capacity, assigns an instructor. After this ticket, batch-creation is ready for placements (ticket 03) and sessions (ticket 05) to consume.

**Blocked by:** 00, 01, 02 (identity for tenant context + grants)

**Status:** ready-for-agent

- [ ] `POST /api/v1/programs` creates a `Program` with `program_code` (unique per unit), `name`, `service_group`, `status='draft'`.
- [ ] `POST /api/v1/programs/{id}/versions` creates a `ProgramVersion` with `version_no`, `learning_objectives`, `curriculum` JSONB, optional `effective_range` `daterange`, `status='draft'`.
- [ ] `POST /api/v1/program-versions/{id}/publish` transitions to `status='published'`, records `published_at`, requires an `approval` decision (X-A08-style `!` action); emits outbox event.
- [ ] The DB-level `btree_gist` exclusion constraint on `program_version.effective_range` rejects overlapping ranges for the same `program_id`; application surfaces as 409.
- [ ] `POST /api/v1/academic-periods` creates an `AcademicPeriod` with `period_kind`, `date_range`, `status='planned'`.
- [ ] `POST /api/v1/batches` creates a `Batch` referencing one `program_version_id`, optional `period_id`, `capacity`, `delivery_mode`, `status='planned'`.
- [ ] `POST /api/v1/batches/{id}/open` transitions `planned → open` once `program_version.status='published'` and at least one instructor is assigned.
- [ ] `PATCH /api/v1/batches/{id}/capacity` lowers or raises capacity; rejects lowering below active-enrollment count unless `force_reason='recorded_exception'` is provided, in which case it succeeds and emits an audit row with the reason (BR03).
- [ ] `POST /api/v1/batches/{id}/close` transitions `open|running → closed`; closed batches do not accept new placements.
- [ ] `POST /api/v1/batches/{id}/teaching-assignments` creates a `TeachingAssignment` with `assignment_kind ∈ {instructor, substitute, assessor, approver}`, optional `session_id`, optional `component_codes[]`, `validity` interval.
- [ ] `POST /api/v1/teaching-assignments/{id}/revoke` sets `status='revoked'`; the grantee's session-scope access drops on the next request.
- [ ] `POST /api/v1/rubrics/{id}/versions` creates a `RubricVersion` against a `program_version_id`, `status='draft'`.
- [ ] `POST /api/v1/rubrics/{id}/indicators` creates `RubricIndicator` rows with `value_kind ∈ {numeric, categorical, narrative}`, `scale` typed JSONB, optional `weight`, `is_required`.
- [ ] `POST /api/v1/rubric-versions/{id}/publish` transitions `draft → published`; subsequent UPDATE/DELETE on the version's indicators is rejected by both the application layer and a trigger.
- [ ] Integration tests for: program-version non-overlap rejection, capacity-lowering rejection below occupancy, rubric-indicator immutability after publish, substitute-instructor scope (cannot see other sessions in the batch).