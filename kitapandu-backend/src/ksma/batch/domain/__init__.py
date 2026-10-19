"""Batch context domain entities."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from enum import StrEnum
from typing import Any
from uuid import UUID, uuid4


class BatchStatus(StrEnum):
    PLANNED = "planned"
    OPEN = "open"
    RUNNING = "running"
    CLOSED = "closed"
    CANCELLED = "cancelled"


class DeliveryMode(StrEnum):
    ONSITE = "onsite"
    ONLINE = "online"
    HYBRID = "hybrid"


class ApplicationStatus(StrEnum):
    DRAFT = "draft"
    SUBMITTED = "submitted"
    UNDER_REVIEW = "under_review"
    APPROVED = "approved"
    REJECTED = "rejected"
    WITHDRAWN = "withdrawn"


class EnrollmentStatus(StrEnum):
    PLANNED = "planned"
    ACTIVE = "active"
    PAUSED = "paused"
    COMPLETED = "completed"
    WITHDRAWN = "withdrawn"
    TRANSFERRED = "transferred"


class ChangeKind(StrEnum):
    TRANSFER = "transfer"
    PAUSE = "pause"
    RESUME = "resume"
    WITHDRAW = "withdraw"


class ChangeStatus(StrEnum):
    PROPOSED = "proposed"
    APPROVED = "approved"
    APPLIED = "applied"
    REJECTED = "rejected"


class AssignmentKind(StrEnum):
    INSTRUCTOR = "instructor"
    SUBSTITUTE = "substitute"
    ASSESSOR = "assessor"
    APPROVER = "approver"


class AssignmentStatus(StrEnum):
    PLANNED = "planned"
    ACTIVE = "active"
    ENDED = "ended"
    REVOKED = "revoked"


@dataclass(slots=True)
class Batch:
    id: UUID = field(default_factory=uuid4)
    tenant_id: UUID = field(default_factory=uuid4)
    unit_id: UUID = field(default_factory=uuid4)
    batch_code: str = ""
    program_version_id: UUID = field(default_factory=uuid4)
    period_id: UUID | None = None
    capacity: int = 0
    delivery_mode: DeliveryMode = DeliveryMode.ONSITE
    service_from: date | None = None
    service_until: date | None = None
    status: BatchStatus = BatchStatus.PLANNED
    created_at: datetime | None = None
    created_by: str = ""
    updated_at: datetime | None = None
    updated_by: str = ""


@dataclass(slots=True)
class Completeness:
    """Per §3.17 Application.completeness: items with `missing`/`supplied`/`verified`.

    Stored as JSONB.
    """

    items: list[dict[str, Any]] = field(default_factory=list)

    def to_json(self) -> list[dict[str, Any]]:
        return list(self.items)

    @classmethod
    def from_json(cls, data: list[dict[str, Any]] | None) -> "Completeness":
        return cls(items=list(data or []))


@dataclass(slots=True)
class Application:
    id: UUID = field(default_factory=uuid4)
    tenant_id: UUID = field(default_factory=uuid4)
    unit_id: UUID = field(default_factory=uuid4)
    participant_id: UUID = field(default_factory=uuid4)
    program_id: UUID = field(default_factory=uuid4)
    requested_start: date | None = None
    completeness: Completeness = field(default_factory=Completeness)
    status: ApplicationStatus = ApplicationStatus.DRAFT
    decision: dict[str, Any] | None = None
    source_ref: str | None = None
    created_at: datetime | None = None
    created_by: str = ""
    updated_at: datetime | None = None
    updated_by: str = ""


@dataclass(slots=True)
class Enrollment:
    id: UUID = field(default_factory=uuid4)
    tenant_id: UUID = field(default_factory=uuid4)
    unit_id: UUID = field(default_factory=uuid4)
    participant_id: UUID = field(default_factory=uuid4)
    batch_id: UUID = field(default_factory=uuid4)
    application_id: UUID | None = None
    effective_from: datetime | None = None
    effective_until: datetime | None = None
    status: EnrollmentStatus = EnrollmentStatus.PLANNED
    policy_id: UUID | None = None
    created_at: datetime | None = None
    created_by: str = ""
    updated_at: datetime | None = None
    updated_by: str = ""


@dataclass(slots=True)
class EnrollmentChange:
    id: UUID = field(default_factory=uuid4)
    tenant_id: UUID = field(default_factory=uuid4)
    source_enrollment_id: UUID = field(default_factory=uuid4)
    target_enrollment_id: UUID | None = None
    change_kind: ChangeKind = ChangeKind.PAUSE
    effective_at: datetime | None = None
    decision: dict[str, Any] = field(default_factory=dict)
    status: ChangeStatus = ChangeStatus.PROPOSED
    created_at: datetime | None = None
    created_by: str = ""
    updated_at: datetime | None = None
    updated_by: str = ""


@dataclass(slots=True)
class TeachingAssignment:
    id: UUID = field(default_factory=uuid4)
    tenant_id: UUID = field(default_factory=uuid4)
    unit_id: UUID = field(default_factory=uuid4)
    membership_id: UUID = field(default_factory=uuid4)
    batch_id: UUID | None = None
    session_id: UUID | None = None
    assignment_kind: AssignmentKind = AssignmentKind.INSTRUCTOR
    component_codes: list[str] = field(default_factory=list)
    valid_from: datetime | None = None
    valid_until: datetime | None = None
    status: AssignmentStatus = AssignmentStatus.PLANNED
    created_at: datetime | None = None
    created_by: str = ""
    updated_at: datetime | None = None
    updated_by: str = ""