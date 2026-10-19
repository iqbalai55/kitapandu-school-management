# Spec 003 — Program and Class Bounded Context

> `Program`, `ProgramVersion`, `AcademicPeriod`, `RubricVersion`, `RubricIndicator`,
> `Batch`, `TeachingAssignment`. PRD module codes CLS01–04, ASM01–02, R03, R10, V08.

## Problem Statement

A school runs programs (a "Bahasa Inggris A1" course), and each program has
versions that evolve over time. Batches are concrete runs of a version. A batch
has capacity, instructors, a period, a location/mode. The system needs to version
programs so that updating the curriculum doesn't silently rewrite history; needs
to version rubrics so that assessments recorded against a published rubric keep
the rubric they used; and needs to enforce that a batch references exactly one
`ProgramVersion`.

## Solution

A `program` bounded context that owns `Program`, `ProgramVersion`,
`AcademicPeriod`, `RubricVersion`, `RubricIndicator`, `Batch`, and the
batch-scope `TeachingAssignment` (the session-scope `TeachingAssignment` is
also in this context per the existing layout). The split mirrors PRD module
codes CLS01–04 + ASM01–02.

## User Stories

### Program and version
1. As **AC**, I want to create a `Program` with a `program_code` (unique per unit),
   `name`, `service_group` (`language`/`health`/`technology`/config), so that the
   program shows up in the catalog.
2. As **AC**, I want a program to have at most one `current_version_id`, which
   is the default for new batches, so that staff have a sensible default.
3. As **AC**, I want a new `ProgramVersion` to record `learning_objectives`,
   `curriculum`, optional `effective_range`, and a `status` (`draft`/`published`/`retired`),
   so that a batch can pin a specific version.
4. As **AC**, I want updating a program to NOT change historical outcomes —
   batches still reference the version they were created with.
5. As **AC**, I want to retire a published `ProgramVersion` without losing
   historical references; existing batches continue, new batches must pick a
   non-retired version.

### Academic period
6. As **AC**, I want to create an `AcademicPeriod` (`period_code`, `name`,
   `period_kind` ('course_window'/'academic_year'/'semester'/config), `date_range`,
   `status`) so that batches have a meaningful window.
7. As **AC**, I want closing an academic period to NOT auto-graduate participants
   or wipe debt — graduation is a separate decision (PRD §4.7).

### Rubric
8. As **AC/AS**, I want to create a `RubricVersion` (`program_version_id`,
   `version_no`, `name`, `status`) so that assessment uses a fixed rubric.
9. As **AC/AS**, I want `RubricIndicator` rows (`indicator_code`, `label`,
   `value_kind` ('numeric'|'categorical'|'narrative'), `scale`, `weight?`,
   `is_required`) so that assessments are typed.
10. As **AC/AS**, I want the `scale` to match `value_kind`: numeric bounds,
    category list, or narrative length.
11. As any **user**, I want a used rubric's indicators to be immutable, so
    that an assessment in flight can't be invalidated by a later rubric change.
12. As **AS**, I want `published_at` and `approval` decision required before
    `status='published'` so that rubrics go through review.

### Batch
13. As **AC**, I want to create a `Batch` with `batch_code` (unique per unit),
    `program_version_id`, optional `period_id`, `capacity`, `delivery_mode`
    ('onsite'/'online'/'hybrid'), optional `service_range`, `status` so that a
    cohort can be scheduled.
14. As **AC**, I want capacity and delivery_mode to be locked once the batch has
    active enrollments, except via a recorded correction (BR03).
15. As **AC**, I want `status` transitions: `planned → open → running → closed`
    or `cancelled`, with each transition requiring a reason.
16. As **AC**, I want a closed batch to NOT accept new placements unless
    reopened by an authorized party.

### Teaching assignment
17. As **AC**, I want to assign an instructor to a whole batch, with optional
    `component_codes` to scope which assessment rubric components they're
    responsible for.
18. As **AC**, I want to assign a substitute instructor scoped to a specific
    session (`session_id` filled) so that the original instructor's access
    ends at the substitute's `valid_until`.
19. As **AC**, I want a substitute's access to be limited to that session; they
    cannot see the whole batch or whole tenant.
20. As **AC**, I want the `assignment_kind` to be one of `instructor`,
    `substitute`, `assessor`, `approver` — these are separate roles and not
    interchangeable.

## Implementation Decisions

### Schema (already migrated, see `0004_program.py` and `0005_batch.py`)
- `program(id, tenant_id, unit_id, program_code, name, service_group, status, current_version_id?, …)`.
  Unique `(tenant_id, unit_id, program_code)`.
- `program_version(id, tenant_id, unit_id, program_id, version_no, learning_objectives, curriculum JSONB,
  effective_range daterange?, status, …)`. Unique `(program_id, version_no)`.
- `academic_period(id, tenant_id, unit_id, period_code, name, period_kind, date_range daterange, status, …)`.
  Unique `(tenant_id, unit_id, period_code)`.
- `rubric_version(id, tenant_id, unit_id, program_version_id, version_no, name, status,
  published_at?, approval JSONB?, …)`. Unique `(program_version_id, version_no)`.
- `rubric_indicator(id, tenant_id, unit_id, rubric_version_id, indicator_code, label, value_kind,
  scale JSONB, weight?, is_required, …)`. Unique `(rubric_version_id, indicator_code)`.
- `batch(id, tenant_id, unit_id, batch_code, program_version_id, period_id?, capacity, delivery_mode,
  service_range daterange?, status, …)`. Unique `(tenant_id, unit_id, batch_code)`.
- `teaching_assignment(id, tenant_id, unit_id, membership_id, batch_id, session_id?,
  assignment_kind, component_codes[], validity tstzrange, status, …)`. CHECK: if `session_id`
  is filled, it must belong to `batch_id` in the same tenant/unit.

### Versioning
- `program_version.effective_range` is a `daterange` (inclusive end, no time-of-day).
- Two `program_version` rows for the same `(program_id)` may have non-overlapping
  `effective_range`; an exclusion constraint enforces it via `btree_gist`.
- `current_version_id` on `program` is updated via `program.publish_version(version_id)`
  and emits `ProgramVersionPublished` outbox event.

### Capacity invariant (R03, BR03)
- Capacity changes go through `batch.update_capacity(new_capacity, *, reason)`.
  The function rejects lowering capacity below the count of active enrollments on
  the current `effective_range`, or requires an explicit `force_reason`.
- The check uses `SELECT count(*) FROM enrollment WHERE batch_id=:bid AND tenant_id=:tid
  AND status IN ('planned','active') AND effective_range && tstzrange(now(), infinity)`.

### Rubric immutability (R10)
- `rubric_indicator` UPDATE/DELETE on a published `rubric_version` is rejected by
  an `is_published` trigger or by the application layer in `RubricVersionRepository.update`.
- Adding a new indicator after `status='published'` is also rejected — instead
  publish a new `RubricVersion`.

### Seams
- **Inbound**: assessment imports `rubric_version` and `rubric_indicator` by ID;
  session imports `teaching_assignment` by ID; finance reads `enrollment_id` only.
- **Outbound**: program depends only on `shared/*`.
- **ACL exports**: `program.acl.teaching_assignments_for_membership(membership_id, *, at=None)`
  used by the session BC.

## Testing Decisions

What makes a good test:
- Asserts version immutability: publish a rubric, attempt UPDATE on its label → 409.
- Asserts capacity decrease: try to lower `capacity` below active enrollments →
  409 `CAPACITY_BELOW_OCCUPANCY`; with `force_reason='recorded_exception'` → succeeds,
  audit row written.
- Asserts version replacement: a new `program_version` with overlapping
  `effective_range` is rejected.
- Asserts substitute scope: `IN` user assigned as substitute on session X cannot
  read attendance for session Y (requires RBAC test).
- Asserts `TeachingAssignment.session_id` consistency: trying to assign instructor
  to a session that doesn't belong to the same batch → 400.

## Out of Scope

- Cross-program curriculum reuse (each program owns its own curriculum).
- Dynamic rubrics beyond the typed `scale` model.
- Auto-promotion of period (`academic_year` to `semester`).
- Foreign-language localization of program names.

## Further Notes

- The existing `src/ksma/program/` folder (~2,900 lines) covers programs, periods,
  batches, rubrics, and the batch-scope teaching assignments. The session-scope
  teaching assignments and the related cross-BC events live alongside.
- The session BC owns `Session`, `SessionRevision`, `Attendance`, `TeachingJournal`,
  `SessionCompletion` — those are not in this BC. See spec 004.
- Remaining work:
  1. Add the `btree_gist` exclusion constraint for non-overlapping
     `program_version.effective_range`.
  2. Implement `batch.update_capacity` with the recorded-exception path.
  3. Add unit tests for `Scale.validate_value` (numeric range, category list,
     narrative bounds).
  4. Add integration tests for capacity and version invariants.