"""Program domain entities + value objects."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any
from uuid import UUID, uuid4


class ProgramStatus(StrEnum):
    DRAFT = "draft"
    ACTIVE = "active"
    INACTIVE = "inactive"


class ProgramVersionStatus(StrEnum):
    DRAFT = "draft"
    PUBLISHED = "published"
    RETIRED = "retired"


class PeriodKind(StrEnum):
    COURSE_WINDOW = "course_window"
    ACADEMIC_YEAR = "academic_year"
    SEMESTER = "semester"


class PeriodStatus(StrEnum):
    PLANNED = "planned"
    ACTIVE = "active"
    CLOSED = "closed"


class RubricVersionStatus(StrEnum):
    DRAFT = "draft"
    PUBLISHED = "published"
    RETIRED = "retired"


class IndicatorValueKind(StrEnum):
    NUMERIC = "numeric"
    CATEGORICAL = "categorical"
    NARRATIVE = "narrative"


class ServiceGroup(StrEnum):
    LANGUAGE = "language"
    HEALTH = "health"
    TECHNOLOGY = "technology"


class DeliveryMode(StrEnum):
    ONSITE = "onsite"
    ONLINE = "online"
    HYBRID = "hybrid"


class BatchStatus(StrEnum):
    PLANNED = "planned"
    OPEN = "open"
    RUNNING = "running"
    CLOSED = "closed"
    CANCELLED = "cancelled"


class TeachingAssignmentKind(StrEnum):
    INSTRUCTOR = "instructor"
    SUBSTITUTE = "substitute"
    ASSESSOR = "assessor"
    APPROVER = "approver"


@dataclass(slots=True)
class Curriculum:
    """Per §3.14: components and planned duration per program.

    Stored as JSONB; shape: ``{"components": [{"code", "name", "hours"}], ...,
    "duration_unit": "minutes" | "hours" | "sessions"}``.
    """

    components: list[dict[str, Any]] = field(default_factory=list)
    duration_unit: str = "hours"

    def to_json(self) -> dict[str, Any]:
        return {"components": self.components, "duration_unit": self.duration_unit}

    @classmethod
    def from_json(cls, data: dict[str, Any] | None) -> "Curriculum":
        if not data:
            return cls()
        return cls(
            components=list(data.get("components") or []),
            duration_unit=str(data.get("duration_unit") or "hours"),
        )


@dataclass(slots=True)
class Program:
    id: UUID = field(default_factory=uuid4)
    tenant_id: UUID = field(default_factory=uuid4)
    unit_id: UUID = field(default_factory=uuid4)
    program_code: str = ""
    name: str = ""
    service_group: str = ""
    status: ProgramStatus = ProgramStatus.DRAFT
    current_version_id: UUID | None = None
    created_at: datetime | None = None
    created_by: str = ""
    updated_at: datetime | None = None
    updated_by: str = ""


@dataclass(slots=True)
class ProgramVersion:
    id: UUID = field(default_factory=uuid4)
    tenant_id: UUID = field(default_factory=uuid4)
    unit_id: UUID = field(default_factory=uuid4)
    program_id: UUID = field(default_factory=uuid4)
    version_no: int = 1
    learning_objectives: str = ""
    curriculum: Curriculum = field(default_factory=Curriculum)
    effective_from: datetime | None = None
    effective_until: datetime | None = None
    status: ProgramVersionStatus = ProgramVersionStatus.DRAFT
    created_at: datetime | None = None
    created_by: str = ""
    updated_at: datetime | None = None
    updated_by: str = ""


@dataclass(slots=True)
class AcademicPeriod:
    id: UUID = field(default_factory=uuid4)
    tenant_id: UUID = field(default_factory=uuid4)
    unit_id: UUID = field(default_factory=uuid4)
    period_code: str = ""
    name: str = ""
    period_kind: PeriodKind = PeriodKind.COURSE_WINDOW
    start_date: datetime | None = None
    end_date: datetime | None = None
    status: PeriodStatus = PeriodStatus.PLANNED
    created_at: datetime | None = None
    created_by: str = ""
    updated_at: datetime | None = None
    updated_by: str = ""


@dataclass(slots=True)
class Scale:
    """Per §3.27 / structured types: Scale follows ``value_kind``.

    - numeric: ``{"min": 0, "max": 100}``
    - categorical: ``{"categories": ["A","B","C"]}``
    - narrative: ``{"max_chars": 1000}``
    """

    min: float | None = None
    max: float | None = None
    categories: list[str] = field(default_factory=list)
    max_chars: int | None = None

    def to_json(self) -> dict[str, Any]:
        out: dict[str, Any] = {}
        if self.min is not None:
            out["min"] = self.min
        if self.max is not None:
            out["max"] = self.max
        if self.categories:
            out["categories"] = list(self.categories)
        if self.max_chars is not None:
            out["max_chars"] = self.max_chars
        return out

    @classmethod
    def from_json(cls, data: dict[str, Any] | None) -> "Scale":
        if not data:
            return cls()
        return cls(
            min=data.get("min"),
            max=data.get("max"),
            categories=list(data.get("categories") or []),
            max_chars=data.get("max_chars"),
        )

    def validate_value(self, value: Any) -> bool:
        if self.min is not None and self.max is not None and isinstance(value, (int, float)):
            return self.min <= value <= self.max
        if self.categories and isinstance(value, str):
            return value in self.categories
        return True


@dataclass(slots=True)
class RubricVersion:
    id: UUID = field(default_factory=uuid4)
    tenant_id: UUID = field(default_factory=uuid4)
    unit_id: UUID = field(default_factory=uuid4)
    program_version_id: UUID = field(default_factory=uuid4)
    version_no: int = 1
    name: str = ""
    status: RubricVersionStatus = RubricVersionStatus.DRAFT
    published_at: datetime | None = None
    approval: dict[str, Any] | None = None
    created_at: datetime | None = None
    created_by: str = ""
    updated_at: datetime | None = None
    updated_by: str = ""


@dataclass(slots=True)
class RubricIndicator:
    id: UUID = field(default_factory=uuid4)
    tenant_id: UUID = field(default_factory=uuid4)
    rubric_version_id: UUID = field(default_factory=uuid4)
    indicator_code: str = ""
    label: str = ""
    value_kind: IndicatorValueKind = IndicatorValueKind.NUMERIC
    scale: Scale = field(default_factory=Scale)
    weight: float | None = None
    is_required: bool = True
    created_at: datetime | None = None
    created_by: str = ""
    updated_at: datetime | None = None
    updated_by: str = ""