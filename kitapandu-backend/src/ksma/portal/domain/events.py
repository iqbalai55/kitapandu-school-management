"""Portal domain events."""
from __future__ import annotations

from ksma.shared.events import DomainEvent


class ServiceRequestSubmitted(DomainEvent):
    event_type = "ServiceRequestSubmitted"