"""KSMA Session bounded context.

Owns:
- Session (logical meeting identity)
- SessionRevision (effective schedule version per ADR05)
- Attendance (per-session per-enrollment; unique; ``belum_diisi`` ≠ absent)
- TeachingJournal (topic + activities + portal/internal note split)
- SessionCompletion (closure or closure revision with ``supersedes_id`` chain)

R09 invariants enforced:
- Unique attendance per (session, enrollment) — composite UNIQUE index
- ``attendance_code IS NULL`` ⇒ status ``belum_diisi`` (NOT absent)
- Closure requires every eligible enrollment + journal.topic + actual_instructor
- Post-close correction chains via ``SessionCompletion.supersedes_id`` and emits
  ``AttendanceCorrected`` events
- Cancellation flows ``lifecycle_status='cancelled'``; cancelled sessions do not
  count as completed (BR04)
"""
from __future__ import annotations