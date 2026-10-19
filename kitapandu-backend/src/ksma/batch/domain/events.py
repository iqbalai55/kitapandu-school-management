"""Batch domain events."""
from __future__ import annotations

from ksma.shared.events import DomainEvent


class BatchCreated(DomainEvent):
    event_type = "BatchCreated"


class BatchOpened(DomainEvent):
    event_type = "BatchOpened"


class BatchClosed(DomainEvent):
    event_type = "BatchClosed"


class BatchCapacityChanged(DomainEvent):
    event_type = "BatchCapacityChanged"


class BatchCapacityExceptionRecorded(DomainEvent):
    """Q46 audit event when capacity is lowered below active occupancy with force."""

    event_type = "BatchCapacityExceptionRecorded"


class ApplicationCreated(DomainEvent):
    event_type = "ApplicationCreated"


class ApplicationSubmitted(DomainEvent):
    event_type = "ApplicationSubmitted"


class ApplicationApproved(DomainEvent):
    event_type = "ApplicationApproved"


class ApplicationRejected(DomainEvent):
    event_type = "ApplicationRejected"


class ApplicationWithdrawn(DomainEvent):
    event_type = "ApplicationWithdrawn"


class ParticipantEnrolled(DomainEvent):
    event_type = "ParticipantEnrolled"


class EnrollmentTransferred(DomainEvent):
    event_type = "EnrollmentTransferred"


class EnrollmentPaused(DomainEvent):
    event_type = "EnrollmentPaused"


class EnrollmentResumed(DomainEvent):
    event_type = "EnrollmentResumed"


class EnrollmentWithdrawn(DomainEvent):
    event_type = "EnrollmentWithdrawn"


class TeachingAssignmentCreated(DomainEvent):
    event_type = "TeachingAssignmentCreated"


class TeachingAssignmentEnded(DomainEvent):
    event_type = "TeachingAssignmentEnded"