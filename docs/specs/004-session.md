# Spec 004 — Session Bounded Context

> `Session`, `SessionRevision`, `Attendance`, `TeachingJournal`, `SessionCompletion`.
> PRD module codes SES01–04, BR04, BR05, R08, R09, V08, V09.

## Problem Statement

A scheduled meeting happens (or doesn't). Attendance is recorded (or not).
A teaching journal describes what happened. The session is closed. Later, the
instructor wants to correct something — and the prior values must remain
traceable. The session schedule can be revised, but at any moment only one
revision is the effective one.

## Solution

A `session` bounded context that owns the learning-delivery lifecycle. The
core invariant is **one effective schedule per session** (R08) and **one
effective attendance per `(session, enrollment)` pair** (R09). Closure is
explicit, never automatic. Corrections are versioned, never silent.

## User Stories

### Session scheduling
1. As **AC**, I want to create a `Session` for a batch with `session_no`
   (positive, unique within batch) so that the batch has a meeting plan.
2. As **AC**, I want to add a `SessionRevision` (`starts_at`, `ends_at`,
   `timezone` IANA, optional `location`, `change_reason`) so that the
   actual schedule is recorded.
3. As **AC**, I want to revise the schedule by adding a new `SessionRevision`
   and atomically swapping the pointer — never two effective revisions (R08).
4. As **AC**, I want recurring sessions to show a preview of generated dates
   + conflicts before save (UX F02), so that I can fix before committing.
5. As **AC**, I want a session in `status='cancelled'` to never have two active
   entries on the calendar (R08), and to never be counted as delivered
   service (BR04).
6. As **AC**, I want a `starts_at < ends_at` check (R05), so that I can't
   save an invalid interval.

### Attendance
7. As **IN**, I want to record attendance per participant with an explicit
   code: `hadir`, `izin`, `sakit`, `tidak_hadir`, `terlambat`, so that the
   reason for absence is auditable.
8. As **IN**, I want `belum_diisi` to be a data-completeness state, NOT
   automatic absence (BR05), so that I don't accidentally mark everyone
   absent just because I haven't recorded them.
9. As **IN**, I want one effective attendance record per `(session, enrollment)`
   pair (unique constraint), so that there's never a duplicate.
10. As **IN**, I want my attendance record to be editable until session
    closure; after closure, corrections require `correction_reason` and
    go through `SES-A06` (recorded).
11. As **AC**, I want to see a recap that distinguishes `tidak_hadir`,
    `belum_diisi`, and `tidak dijadwalkan` (UX F03), so that I can target
    follow-up correctly.
12. As **IN**, I want a session to not accept attendance for an enrollment
    not active at the session's date (V08), so that retired students can't
    pollute the record.

### Teaching journal
13. As **IN**, I want to record a teaching journal with `topic`, `activities`,
    `internal_note` (restricted access), `portal_summary` (portal-readable),
    `follow_up`, so that the session has a complete narrative.
14. As **IN**, I want `actual_instructor_id` recorded even after a substitute
    takes over, so that the audit trail shows who actually taught.
15. As **IN**, I want the journal to be saved as draft without it being
    published to the portal (drafts ≠ published, BR11).

### Session closure
16. As **IN**, I want to close a session via `SES-A05` once attendance and
    journal are complete; the closure decision is recorded in
    `SessionCompletion` with `completion_no` (positive version), `decision`,
    `checklist_snapshot`, `policy_id`.
17. As **IN**, I want the system to refuse closure if mandatory journal items
    are missing, so that I can't accidentally close a session with no journal
    (V09).
18. As **IN**, I want past sessions to NOT auto-close just because calendar
    time elapsed, so that I get to record what actually happened (BR05).
19. As **AC/IN**, I want to correct a closed session via `SES-A06` with a
    `correction_reason`; the old decision is preserved with `supersedes_id`
    and the new one becomes effective.
20. As any **user**, I want to read both the prior and current `SessionCompletion`
    versions, so that the audit chain is complete.

### Substitute instructor
21. As **AC**, I want to assign a substitute instructor via `SES-A08` for a
    specific session and time range; the substitute sees the session, the
    original instructor's access ends at `valid_until`.
22. As **AC**, I want a substitute to NOT see other sessions in the batch or
    other batches in the unit, so that the access matrix's narrow scope is
    enforced.

## Implementation Decisions

### Schema (already migrated, see `alembic/versions/0006_session.py`)
- `session(id, tenant_id, unit_id, batch_id, session_no, effective_revision_id?, lifecycle_status
  ('planned'|'scheduled'|'cancelled'|'closed'), closure_id?, reschedule_reason?, …)`.
  Unique `(batch_id, session_no)`. CHECK `starts_at < ends_at` enforced via the
  `effective_revision`.
- `session_revision(id, tenant_id, unit_id, session_id, revision_no, starts_at, ends_at,
  timezone, location JSONB?, change_reason, published_at?, …)`. Unique `(session_id, revision_no)`.
- `attendance(id, tenant_id, unit_id, session_id, enrollment_id, attendance_code?, note?,
  recorded_at?, recorded_by?, correction_reason?, …)`. Unique `(tenant_id, session_id, enrollment_id)`.
- `teaching_journal(id, tenant_id, unit_id, session_id, actual_instructor_id, topic, activities?,
  internal_note?, portal_summary?, follow_up?, …)`. Unique `(session_id)`.
- `session_completion(id, tenant_id, unit_id, session_id, completion_no, decision JSONB,
  checklist_snapshot JSONB, policy_id, supersedes_id?, correction_reason?, …)`.
  Unique `(session_id, completion_no)`.

### One-effective-revision invariant (R08)
- `session.effective_revision_id` is the only pointer used by reads.
- `session.append_revision(rev)` runs `BEGIN; UPDATE session SET effective_revision_id=:rev
  WHERE id=:sid AND tenant_id=:tid; UPDATE session_revision SET published_at=now() WHERE id=:rev;
  COMMIT;`.
- Old revisions remain queryable for history; `published_at` marks when they were effective.
- Cancellation: `session.cancel(reason)` sets `lifecycle_status='cancelled'` and keeps the
  effective revision but flags it as cancelled in the calendar view.

### Attendance uniqueness + closure correction (V08, V09)
- `attendance_create` enforces `enrollment.effective_range @> session.starts_at` (V08) inside
  the same transaction; otherwise 409 `ENROLLMENT_NOT_ACTIVE_AT_SESSION`.
- `attendance_update` after closure requires `correction_reason` (≥1 char) and runs inside
  the same transaction as the prior-row copy into `attendance_history` (or `supersedes_id`
  on a parallel row if the data dictionary adopts that pattern).

### Closure prerequisite check (V09)
- `session.close(actor, *, checklist)` verifies:
  - all enrollments in `batch` at session time have an attendance row (even if `belum_diisi`)
  - `teaching_journal` has `topic` and at least one of `activities`, `portal_summary`,
    `follow_up`.
  - Then writes `SessionCompletion` with `completion_no = MAX(...)+1`.
- If any prerequisite missing → 409 `INCOMPLETE_CLOSURE_CHECKLIST`.

### Substitute scope
- Substitute's `teaching_assignment.session_id` is filled; the session BC filters reads to
  that assignment's `session_id` for `IN` users holding only `substitute` kind.
- The `require_action(SES_A01, target=session)` resolver compares the user's session-scope
  `TeachingAssignment` against the target session.

### Seams
- **Inbound**: assessment reads `attendance` summaries; reporting reads session counts.
- **Outbound**: session depends on `program.domain.Batch` (FK), `participant.domain.Enrollment`
  (FK), `identity.domain.Membership` (for actual_instructor).
- **ACL exports**: `session.acl.attendance_for_session(session_id, requester_id, scope)`
  returns only what's visible given the requester's grants.

## Testing Decisions

What makes a good test:
- Asserts **effective_revision swap**: `session.effective_revision_id` is exactly the new
  revision after `append_revision`; the old revision still exists with `published_at`.
- Asserts **one attendance per pair**: a second `attendance_create` on the same
  `(session, enrollment)` returns 409.
- V08: trying to record attendance for an enrollment that was not active at the session's
  `starts_at` returns 409 `ENROLLMENT_NOT_ACTIVE_AT_SESSION`.
- V09: closing with empty journal returns 409 `INCOMPLETE_CLOSURE_CHECKLIST`; the session
  stays in `lifecycle_status='scheduled'`.
- BR04: cancelled session's attendance does not count as "delivered service" — the
  session-list API returns it but flags `counted_as_delivered=false`.
- Substitute: `IN` user assigned to one session via `assignment_kind='substitute'` cannot
  read attendance on another session in the same batch.

## Out of Scope

- Recurring-session generator as a built-in cron job (preview-and-commit only for P0).
- Mobile offline save (PRD defers full offline to P1+).
- Auto-close based on calendar time.
- Push notifications to participants (Q09 pending).

## Further Notes

- The existing `src/ksma/session/` folder (~2,900 lines) covers the lifecycle.
- Remaining work:
  1. Verify `session.append_revision` uses a single transaction (existing code may
     rely on application-level sequencing).
  2. Implement the closure prerequisite check end-to-end.
  3. Add unit tests for `attendance_codes` mapping and the closure-checklist rule.
  4. Add integration tests for V08, V09, R08.