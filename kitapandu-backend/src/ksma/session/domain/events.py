"""Session domain events."""
from __future__ import annotations

from ksma.shared.events import DomainEvent


class SessionCreated(DomainEvent):
    event_type = "SessionCreated"


class SessionScheduled(DomainEvent):
    event_type = "SessionScheduled"


class SessionRescheduled(DomainEvent):
    event_type = "SessionRescheduled"


class SessionCancelled(DomainEvent):
    event_type = "SessionCancelled"


class SessionClosed(DomainEvent):
    event_type = "SessionClosed"


class SessionCorrected(DomainEvent):
    event_type = "SessionCorrected"


class AttendanceRecorded(DomainEvent):
    event_type = "AttendanceRecorded"


class AttendanceCorrected(DomainEvent):
    event_type = "AttendanceCorrected"


class TeachingJournalWritten(DomainEvent):
    event_type = "TeachingJournalWritten"


class SubstituteInstructorAssigned(DomainEvent):
    event_type = "SubstituteInstructorAssigned"