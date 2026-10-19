"""Session domain entities."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any
from uuid import UUID, uuid4


class SessionLifecycleStatus(StrEnum):
    PLANNED = "planned"
    SCHEDULED = "scheduled"
    CANCELLED = "cancelled"
    CLOSED = "closed"


class AttendanceCode(StrEnum):
    HADIR = "hadir"
    IZIN = "izin"
    SAKIT = "sakit"
    TIDAK_HADIR = "tidak_hadir"
    TERLAMBAT = "terlambat"
    # ``belum_diisi`` is represented as ``attendance_code IS NULL`` (NOT a value).


class CompletionDecision(StrEnum):
    """Closure outcomes on ``SessionCompletion``."""

    COMPLETED = "completed"
    INCOMPLETE = "incomplete"
    REOPENED = "reopened"


@dataclass(slots=True)
class Session:
    id: UUID = field(default_factory=uuid4)
    tenant_id: UUID = field(default_factory=uuid4)
    unit_id: UUID = field(default_factory=uuid4)
    batch_id: UUID = field(default_factory=uuid4)
    session_no: int = 0
    effective_revision_id: UUID | None = None
    lifecycle_status: SessionLifecycleStatus = SessionLifecycleStatus.PLANNED
    closure_id: UUID | None = None
    reschedule_reason: str | None = None
    created_at: datetime | None = None
    created_by: str = ""
    updated_at: datetime | None = None
    updated_by: str = ""


@dataclass(slots=True)
class SessionRevision:
    id: UUID = field(default_factory=uuid4)
    tenant_id: UUID = field(default_factory=uuid4)
    session_id: UUID = field(default_factory=uuid4)
    revision_no: int = 1
    starts_at: datetime | None = None
    ends_at: datetime | None = None
    timezone: str = "Asia/Jakarta"
    location: dict[str, Any] | None = None
    change_reason: str = ""
    published_at: datetime | None = None
    created_at: datetime | None = None
    created_by: str = ""
    updated_at: datetime | None = None
    updated_by: str = ""


@dataclass(slots=True)
class Attendance:
    """Per-session per-enrollment. Unique by composite index.

    ``attendance_code IS NULL`` means ``belum_diisi`` (NOT absent). Recording an
    attendance value sets ``recorded_at`` + ``recorded_by``. Post-close
    corrections must set ``correction_reason`` and trigger ``AttendanceCorrected``.
    """

    id: UUID = field(default_factory=uuid4)
    tenant_id: UUID = field(default_factory=uuid4)
    session_id: UUID = field(default_factory=uuid4)
    enrollment_id: UUID = field(default_factory=uuid4)
    attendance_code: AttendanceCode | None = None
    note: str | None = None
    recorded_at: datetime | None = None
    recorded_by: str | None = None
    correction_reason: str | None = None
    created_at: datetime | None = None
    created_by: str = ""
    updated_at: datetime | None = None
    updated_by: str = ""


@dataclass(slots=True)
class TeachingJournal:
    id: UUID = field(default_factory=uuid4)
    tenant_id: UUID = field(default_factory=uuid4)
    session_id: UUID = field(default_factory=uuid4)
    actual_instructor_id: UUID = field(default_factory=uuid4)
    topic: str = ""
    activities: str | None = None
    internal_note: str | None = None  # restricted (X-A02-style access)
    portal_summary: str | None = None  # public-on-portal summary
    follow_up: str | None = None
    created_at: datetime | None = None
    created_by: str = ""
    updated_at: datetime | None = None
    updated_by: str = ""


@dataclass(slots=True)
class SessionCompletion:
    """Closure (or closure revision) per §3.25. Old decisions preserved via FK chain."""

    id: UUID = field(default_factory=uuid4)
    tenant_id: UUID = field(default_factory=uuid4)
    session_id: UUID = field(default_factory=uuid4)
    completion_no: int = 1
    decision: dict[str, Any] = field(default_factory=dict)
    checklist_snapshot: dict[str, Any] = field(default_factory=dict)
    policy_id: UUID | None = None
    supersedes_id: UUID | None = None
    correction_reason: str | None = None
    outcome: CompletionDecision = CompletionDecision.COMPLETED
    created_at: datetime | None = None
    created_by: str = ""
    updated_at: datetime | None = None
    updated_by: str = ""