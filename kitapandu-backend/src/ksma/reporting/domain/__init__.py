"""Reporting domain entities."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from enum import StrEnum
from typing import Any
from uuid import UUID, uuid4


class ReportStatus(StrEnum):
    PUBLISHED = "published"
    SUPERSEDED = "superseded"
    REVOKED = "revoked"


class CompletionOutcome(StrEnum):
    COMPLETED = "completed"
    REMEDIAL_REQUIRED = "remedial_required"
    NOT_COMPLETED = "not_completed"


class CertificateStatus(StrEnum):
    ISSUED = "issued"
    REPLACED = "replaced"
    REVOKED = "revoked"


@dataclass(slots=True)
class ReportPublication:
    id: UUID = field(default_factory=uuid4)
    tenant_id: UUID = field(default_factory=uuid4)
    unit_id: UUID = field(default_factory=uuid4)
    enrollment_id: UUID = field(default_factory=uuid4)
    publication_no: int = 1
    source_assessment_ids: list[UUID] = field(default_factory=list)
    snapshot: dict[str, Any] = field(default_factory=dict)
    publication: dict[str, Any] = field(default_factory=dict)
    attachment_id: UUID | None = None
    supersedes_id: UUID | None = None
    status: ReportStatus = ReportStatus.PUBLISHED
    created_at: datetime | None = None
    created_by: str = ""
    updated_at: datetime | None = None
    updated_by: str = ""


@dataclass(slots=True)
class CompletionDecision:
    id: UUID = field(default_factory=uuid4)
    tenant_id: UUID = field(default_factory=uuid4)
    unit_id: UUID = field(default_factory=uuid4)
    enrollment_id: UUID = field(default_factory=uuid4)
    outcome: CompletionOutcome = CompletionOutcome.NOT_COMPLETED
    evidence_refs: list[dict[str, Any]] = field(default_factory=list)
    policy_id: UUID | None = None
    decision: dict[str, Any] = field(default_factory=dict)
    supersedes_id: UUID | None = None
    created_at: datetime | None = None
    created_by: str = ""
    updated_at: datetime | None = None
    updated_by: str = ""


@dataclass(slots=True)
class Certificate:
    id: UUID = field(default_factory=uuid4)
    tenant_id: UUID = field(default_factory=uuid4)
    unit_id: UUID = field(default_factory=uuid4)
    completion_id: UUID = field(default_factory=uuid4)
    numbering_scope: str = "default"
    certificate_no: str = ""
    snapshot: dict[str, Any] = field(default_factory=dict)
    attachment_id: UUID | None = None
    status: CertificateStatus = CertificateStatus.ISSUED
    replacement_id: UUID | None = None
    revocation: dict[str, Any] | None = None
    created_at: datetime | None = None
    created_by: str = ""