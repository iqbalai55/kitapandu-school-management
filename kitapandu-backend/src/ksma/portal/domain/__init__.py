"""Portal domain entities."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any
from uuid import UUID, uuid4


class ServiceRequestStatus(StrEnum):
    OPEN = "open"
    IN_REVIEW = "in_review"
    APPROVED = "approved"
    REJECTED = "rejected"
    CLOSED = "closed"


class ServiceRequestCategory(StrEnum):
    ACADEMIC = "academic"
    SCHEDULE = "schedule"
    IDENTITY = "identity"
    FINANCE = "finance"


class RequesterKind(StrEnum):
    ACCOUNT = "account"
    SERVICE = "service"


class NotificationDeliveryState(StrEnum):
    PENDING = "pending"
    AVAILABLE = "available"
    SUPPRESSED = "suppressed"


class NotificationChannel(StrEnum):
    INTERNAL = "internal"
    MANUAL_TEMPLATE = "manual_template"


@dataclass(slots=True)
class ServiceRequest:
    id: UUID = field(default_factory=uuid4)
    tenant_id: UUID = field(default_factory=uuid4)
    unit_id: UUID | None = None
    requester_account_id: UUID = field(default_factory=uuid4)
    requester_kind: RequesterKind = RequesterKind.ACCOUNT
    system_event_id: UUID | None = None
    participant_id: UUID = field(default_factory=uuid4)
    category: ServiceRequestCategory = ServiceRequestCategory.ACADEMIC
    source_ref: dict[str, Any] | None = None
    description: str = ""
    assigned_membership_id: UUID | None = None
    status: ServiceRequestStatus = ServiceRequestStatus.OPEN
    resolution: dict[str, Any] | None = None
    created_at: datetime | None = None
    created_by: str = ""
    updated_at: datetime | None = None
    updated_by: str = ""


@dataclass(slots=True)
class InternalNotification:
    id: UUID = field(default_factory=uuid4)
    tenant_id: UUID = field(default_factory=uuid4)
    recipient_account_id: UUID = field(default_factory=uuid4)
    source_ref: dict[str, Any] = field(default_factory=dict)
    event_id: UUID | None = None
    notification_kind: str = ""
    summary: str = ""
    delivery_state: NotificationDeliveryState = NotificationDeliveryState.PENDING
    read_at: datetime | None = None
    occurred_at: datetime | None = None


@dataclass(slots=True)
class CommunicationPreference:
    id: UUID = field(default_factory=uuid4)
    tenant_id: UUID = field(default_factory=uuid4)
    account_id: UUID = field(default_factory=uuid4)
    participant_id: UUID | None = None
    category: str = ""
    channel: NotificationChannel = NotificationChannel.INTERNAL
    enabled: bool = True
    contact_reference: str | None = None
    created_at: datetime | None = None


__all__ = [
    "ServiceRequest",
    "ServiceRequestStatus",
    "ServiceRequestCategory",
    "RequesterKind",
    "InternalNotification",
    "NotificationDeliveryState",
    "CommunicationPreference",
    "NotificationChannel",
]