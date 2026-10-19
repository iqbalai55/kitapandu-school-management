"""Domain events base + in-process dispatcher.

Per ADR0001 §6 + Round 2 Q12: past-tense aggregate events (``ParticipantEnrolled``,
``FundsAllocated``, ``ReportPublished``). Every event is also written to the
``OutboxEvent`` table in the same transaction as the business write, so the
background worker can fan out notifications, audit projections, and metrics.

In-process handlers run synchronously after the session commits (e.g., the
internal-notification consumer writes an ``InternalNotification`` row). External
handlers (email/SMS) wait for a separate outbox worker process.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, ClassVar
from uuid import UUID, uuid4


@dataclass(frozen=True, slots=True)
class DomainEvent(ABC):
    """Base class for all KSMA domain events.

    Concrete subclasses set ``event_type`` (stable string used by consumers for
    routing + serialization versioning).
    """

    event_type: ClassVar[str] = "DOMAIN_EVENT"
    schema_version: ClassVar[int] = 1

    event_id: UUID = field(default_factory=uuid4)
    occurred_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    correlation_id: UUID | None = None
    actor_kind: str = "account"
    actor_id: UUID | None = None
    tenant_id: UUID | None = None


# A handler is a callable receiving a concrete DomainEvent subclass.
Handler = Callable[[DomainEvent], Any]


class EventBus:
    """Tiny synchronous in-process dispatcher.

    Multiple handlers can subscribe to the same event type; the bus runs them in
    registration order. Cross-process fan-out goes through the OutboxEvent table
    (see ``ksma.shared.outbox``).
    """

    def __init__(self) -> None:
        self._handlers: dict[type[DomainEvent], list[Handler]] = {}

    def subscribe(self, event_type: type[DomainEvent], handler: Handler) -> None:
        self._handlers.setdefault(event_type, []).append(handler)

    def dispatch(self, event: DomainEvent) -> None:
        handlers = self._handlers.get(type(event), [])
        for handler in handlers:
            handler(event)


# Process-wide bus (one per app).
_default_bus = EventBus()


def bus() -> EventBus:
    return _default_bus