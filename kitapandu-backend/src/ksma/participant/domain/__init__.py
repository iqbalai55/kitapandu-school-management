"""Participant domain entities + value objects.

Pure dataclasses; no I/O. Mapping to ORM models lives in
``ksma.participant.infrastructure``.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from enum import StrEnum
from typing import Any
from uuid import UUID, uuid4


class ParticipantStatus(StrEnum):
    ACTIVE = "active"
    INACTIVE = "inactive"
    ARCHIVED = "archived"


class RelatedPartyType(StrEnum):
    PERSON = "person"
    ORGANIZATION = "organization"


class RelatedPartyStatus(StrEnum):
    ACTIVE = "active"
    INACTIVE = "inactive"


class RelationKind(StrEnum):
    """BR02: guardian, payer, sponsor are DIFFERENT relationships.

    ``academic_guardian`` = GU role
    ``payer``            = PY role
    ``sponsor``          = organizational sponsor (Q14 deferred — endpoint stubbed)
    """

    ACADEMIC_GUARDIAN = "academic_guardian"
    PAYER = "payer"
    SPONSOR = "sponsor"


class RelationStatus(StrEnum):
    PENDING = "pending"
    ACTIVE = "active"
    EXPIRED = "expired"
    REVOKED = "revoked"


class LinkStatus(StrEnum):
    PENDING = "pending"
    ACTIVE = "active"
    REVOKED = "revoked"


@dataclass(slots=True)
class Participant:
    """Tenant-local profile. May be referenced cross-unit via Application/Enrollment."""

    id: UUID = field(default_factory=uuid4)
    tenant_id: UUID = field(default_factory=uuid4)
    participant_no: str = ""
    full_name: str = ""
    birth_date: date | None = None
    contact: dict[str, Any] | None = None
    identity_reference: dict[str, Any] | None = None  # {kind, number}; NIK not universal
    status: ParticipantStatus = ParticipantStatus.ACTIVE
    # Encrypted-at-rest per Q37. Set via ``EncryptedNote`` helpers.
    support_needs_note_enc: bytes | None = None
    created_at: datetime | None = None
    created_by: str = ""
    updated_at: datetime | None = None
    updated_by: str = ""


@dataclass(slots=True)
class RelatedParty:
    """A party related to a participant: guardian, payer, or organization sponsor."""

    id: UUID = field(default_factory=uuid4)
    tenant_id: UUID = field(default_factory=uuid4)
    party_type: RelatedPartyType = RelatedPartyType.PERSON
    display_name: str = ""
    contact: dict[str, Any] | None = None
    organization_ref: str | None = None
    status: RelatedPartyStatus = RelatedPartyStatus.ACTIVE
    created_at: datetime | None = None
    created_by: str = ""
    updated_at: datetime | None = None
    updated_by: str = ""


@dataclass(slots=True)
class FinancialScope:
    """Per Q33(a): JSONB shape with two forms.

    ``{"all_approved": true}`` for new payer relations (default; conservative).
    ``{"invoices": [uuid, ...]}`` for explicit lists.
    """

    all_approved: bool = False
    invoices: tuple[UUID, ...] = ()

    def to_json(self) -> dict[str, Any]:
        if self.all_approved:
            return {"all_approved": True}
        return {"invoices": [str(x) for x in self.invoices]}

    @classmethod
    def from_json(cls, data: dict[str, Any] | None) -> "FinancialScope":
        if not data:
            return cls()
        if data.get("all_approved"):
            return cls(all_approved=True)
        return cls(invoices=tuple(UUID(x) for x in data.get("invoices") or ()))

    def covers_invoice(self, invoice_id: UUID) -> bool:
        if self.all_approved:
            return True
        return invoice_id in self.invoices


@dataclass(slots=True)
class ParticipantAccountLink:
    """Verified bridge from Account to either Participant or RelatedParty.

    Exactly one of ``participant_id`` / ``related_party_id`` is set.
    """

    id: UUID = field(default_factory=uuid4)
    tenant_id: UUID = field(default_factory=uuid4)
    account_id: UUID = field(default_factory=uuid4)
    participant_id: UUID | None = None
    related_party_id: UUID | None = None
    verification: dict[str, Any] = field(default_factory=dict)
    valid_from: datetime | None = None
    valid_until: datetime | None = None
    status: LinkStatus = LinkStatus.PENDING
    created_at: datetime | None = None
    created_by: str = ""
    updated_at: datetime | None = None
    updated_by: str = ""


@dataclass(slots=True)
class ParticipantRelation:
    """Academic or financial mandate between Participant and RelatedParty."""

    id: UUID = field(default_factory=uuid4)
    tenant_id: UUID = field(default_factory=uuid4)
    participant_id: UUID = field(default_factory=uuid4)
    related_party_id: UUID = field(default_factory=uuid4)
    relation_kind: RelationKind = RelationKind.ACADEMIC_GUARDIAN
    financial_scope: FinancialScope = field(default_factory=FinancialScope)
    verification: dict[str, Any] = field(default_factory=dict)
    valid_from: datetime | None = None
    valid_until: datetime | None = None
    status: RelationStatus = RelationStatus.PENDING
    created_at: datetime | None = None
    created_by: str = ""
    updated_at: datetime | None = None
    updated_by: str = ""


@dataclass(frozen=True, slots=True)
class DuplicateSuspect:
    """Computed at search time. NOT a stored flag (V02)."""

    participant_id: UUID
    matched_participant_ids: tuple[UUID, ...]
    reason: str  # 'same_name' | 'same_contact_email' | 'same_contact_phone'