"""Domain events emitted by the participant context.

Past-tense aggregate events. Consumers:
- AuditRecord projection (always)
- InternalNotification (when ``relations`` change for a participant)
- Portal sidebar (re-validate reachable participants when a relation is verified)
"""
from __future__ import annotations

from ksma.shared.events import DomainEvent


class ParticipantCreated(DomainEvent):
    event_type = "ParticipantCreated"


class ParticipantUpdated(DomainEvent):
    event_type = "ParticipantUpdated"


class ParticipantArchived(DomainEvent):
    event_type = "ParticipantArchived"


class ParticipantsMerged(DomainEvent):
    """R17 audit event; carries both source and target IDs."""

    event_type = "ParticipantsMerged"


class RelatedPartyCreated(DomainEvent):
    event_type = "RelatedPartyCreated"


class ParticipantAccountLinkRequested(DomainEvent):
    event_type = "ParticipantAccountLinkRequested"


class ParticipantAccountLinkVerified(DomainEvent):
    event_type = "ParticipantAccountLinkVerified"


class ParticipantAccountLinkRevoked(DomainEvent):
    event_type = "ParticipantAccountLinkRevoked"


class ParticipantRelationRequested(DomainEvent):
    event_type = "ParticipantRelationRequested"


class ParticipantRelationVerified(DomainEvent):
    event_type = "ParticipantRelationVerified"


class ParticipantRelationRevoked(DomainEvent):
    event_type = "ParticipantRelationRevoked"


class ParticipantRelationExpired(DomainEvent):
    event_type = "ParticipantRelationExpired"