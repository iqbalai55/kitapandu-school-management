"""Assessment domain entities."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from enum import StrEnum
from typing import Any
from uuid import UUID, uuid4


class AssessmentStatus(StrEnum):
    DRAFT = "draft"
    SUBMITTED = "submitted"
    REVIEWED = "reviewed"
    SUPERSEDED = "superseded"


class ResultState(StrEnum):
    NOT_ASSESSED = "not_assessed"
    ASSESSED = "assessed"
    NOT_APPLICABLE = "not_applicable"


class EvidenceVisibility(StrEnum):
    INTERNAL_ASSESSMENT = "internal_assessment"


@dataclass(slots=True)
class Assessment:
    id: UUID = field(default_factory=uuid4)
    tenant_id: UUID = field(default_factory=uuid4)
    unit_id: UUID = field(default_factory=uuid4)
    enrollment_id: UUID = field(default_factory=uuid4)
    rubric_version_id: UUID = field(default_factory=uuid4)
    assessor_id: UUID = field(default_factory=uuid4)
    assessment_date: date | None = None
    status: AssessmentStatus = AssessmentStatus.DRAFT
    supersedes_id: UUID | None = None
    created_at: datetime | None = None
    created_by: str = ""
    updated_at: datetime | None = None
    updated_by: str = ""


@dataclass(slots=True)
class AssessmentResult:
    """One indicator's value within an Assessment.

    Exactly one of ``numeric_value`` / ``category_code`` / ``narrative_value``
    is set per the indicator's ``value_kind``. The CHECK constraint enforces
    this at the DB level; the application layer validates against ``Scale``.
    """

    id: UUID = field(default_factory=uuid4)
    tenant_id: UUID = field(default_factory=uuid4)
    assessment_id: UUID = field(default_factory=uuid4)
    indicator_id: UUID = field(default_factory=uuid4)
    numeric_value: float | None = None
    category_code: str | None = None
    narrative_value: str | None = None
    result_state: ResultState = ResultState.NOT_ASSESSED
    follow_up: str | None = None
    created_at: datetime | None = None
    created_by: str = ""
    updated_at: datetime | None = None
    updated_by: str = ""


@dataclass(slots=True)
class AssessmentEvidence:
    id: UUID = field(default_factory=uuid4)
    tenant_id: UUID = field(default_factory=uuid4)
    assessment_id: UUID = field(default_factory=uuid4)
    result_id: UUID | None = None
    attachment_id: UUID = field(default_factory=uuid4)
    caption: str | None = None
    visibility: EvidenceVisibility = EvidenceVisibility.INTERNAL_ASSESSMENT
    created_at: datetime | None = None
    created_by: str = ""