"""Program domain events."""
from __future__ import annotations

from ksma.shared.events import DomainEvent


class ProgramCreated(DomainEvent):
    event_type = "ProgramCreated"


class ProgramStatusChanged(DomainEvent):
    event_type = "ProgramStatusChanged"


class ProgramVersionCreated(DomainEvent):
    event_type = "ProgramVersionCreated"


class ProgramVersionPublished(DomainEvent):
    event_type = "ProgramVersionPublished"


class ProgramVersionRetired(DomainEvent):
    event_type = "ProgramVersionRetired"


class AcademicPeriodCreated(DomainEvent):
    event_type = "AcademicPeriodCreated"


class AcademicPeriodActivated(DomainEvent):
    event_type = "AcademicPeriodActivated"


class AcademicPeriodClosed(DomainEvent):
    event_type = "AcademicPeriodClosed"


class RubricVersionCreated(DomainEvent):
    event_type = "RubricVersionCreated"


class RubricVersionPublished(DomainEvent):
    event_type = "RubricVersionPublished"


class RubricIndicatorAdded(DomainEvent):
    event_type = "RubricIndicatorAdded"