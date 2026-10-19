# 05: Session slice — schedule, attendance, journal, closure

**What to build:** A complete session lifecycle: AC schedules a session (with optional recurring preview), IN records attendance, writes the teaching journal, and closes the session. The session's effective revision is the only one visible; corrections are versioned. After this ticket, V08 (attendance for inactive enrollment) and V09 (closure refusal for incomplete journal) are demoable.

**Blocked by:** 00, 01, 02, 03 (enrollments must exist), 04 (batch + teaching assignment must exist)

**Status:** ready-for-agent

- [ ] `POST /api/v1/batches/{id}/sessions` creates a `Session` with `session_no` (unique per batch), `lifecycle_status='planned'`.
- [ ] `POST /api/v1/sessions/{id}/revisions` creates a `SessionRevision` with `starts_at`, `ends_at`, `timezone` IANA, optional `location`, `change_reason`; CHECK `starts_at < ends_at`.
- [ ] `POST /api/v1/sessions/{id}/apply-revision` atomically swaps `session.effective_revision_id` and stamps `published_at` on the new revision; the old revision remains queryable.
- [ ] `POST /api/v1/sessions/{id}/cancel` sets `lifecycle_status='cancelled'` with a `reschedule_reason`; cancelled sessions do not count as delivered service (BR04) and never appear as a second active entry on the calendar.
- [ ] `POST /api/v1/schedules/preview` (CLS-A06) accepts a recurrence rule + holiday exceptions, returns the generated list of date+time slots + detected resource conflicts (instructor, room) BEFORE saving; this is a dry-run, no rows are written.
- [ ] `PUT /api/v1/sessions/{id}/attendance` upserts an `Attendance` row per `(session, enrollment)` with `attendance_code ∈ {hadir, izin, sakit, tidak_hadir, terlambat}`; empty `attendance_code` = `belum_diisi` data-completeness state, NOT automatic absence (BR05); unique on `(tenant_id, session_id, enrollment_id)`.
- [ ] `PUT /api/v1/sessions/{id}/attendance/{enrollment_id}` rejects when the enrollment was not active at the session's `starts_at` (V08).
- [ ] `PUT /api/v1/sessions/{id}/journal` upserts a `TeachingJournal` with `actual_instructor_id` (defaults to the assignee), `topic`, `activities`, `internal_note` (restricted), `portal_summary` (portal-readable), `follow_up`.
- [ ] `POST /api/v1/sessions/{id}/close` (SES-A05) checks the closure checklist (every attendee has an attendance row, journal has `topic` plus at least one of `activities`/`portal_summary`/`follow_up`); writes `SessionCompletion(decision, checklist_snapshot, policy_id, completion_no)`; rejects 409 `INCOMPLETE_CLOSURE_CHECKLIST` if any item missing (V09).
- [ ] `POST /api/v1/sessions/{id}/corrections` (SES-A06) creates a NEW `SessionCompletion` row with `supersedes_id` pointing at the prior version and `correction_reason` ≥ 1 char; the prior row remains queryable.
- [ ] `POST /api/v1/sessions/{id}/substitute-instructor` (SES-A08) creates a `TeachingAssignment(assignment_kind='substitute', session_id=:id)` with explicit validity; the original instructor's session-scope access ends at `valid_until`.
- [ ] `GET /api/v1/sessions/{id}` returns the session with its `effective_revision`, attendance summary (`hadir`, `tidak_hadir`, `belum_diisi`, `tidak_dijadwalkan` distinct counts), journal presence flag, closure status.
- [ ] Integration tests for: effective-revision swap (one effective per session), V08 enrollment-not-active rejection, V09 closure-checklist refusal, substitute scope (cannot read other sessions).