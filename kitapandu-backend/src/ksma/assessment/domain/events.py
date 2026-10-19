"""Assessment domain events."""
from __future__ import annotations

from ksma.shared.events import DomainEvent


class AssessmentCreated(DomainEvent):
    event_type = "AssessmentCreated"


class AssessmentSubmitted(DomainEvent):
    event_type = "AssessmentSubmitted"


class AssessmentReviewed(DomainEvent):
    event_type = "AssessmentReviewed"


class AssessmentSuperseded(DomainEvent):
    event_type = "AssessmentSuperseded"


class AssessmentResultSet(DomainEvent):
    event_type = "AssessmentResultSet"


class AssessmentEvidenceAttached(DomainEvent):
    event_type = "AssessmentEvidenceAttached"