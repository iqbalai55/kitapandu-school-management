"""Operations domain events.

Each event is appended to ``outbox_messages`` in the same transaction as the
business write. Subscribers (e.g., internal-notification writers) react via
``bus().subscribe`` at app startup.
"""
from __future__ import annotations

from ksma.shared.events import DomainEvent


class AttachmentUploaded(DomainEvent):
    event_type = "AttachmentUploaded"


class AttachmentScanned(DomainEvent):
    event_type = "AttachmentScanned"


class AttachmentDownloaded(DomainEvent):
    event_type = "AttachmentDownloaded"


class ImportPreviewed(DomainEvent):
    event_type = "ImportPreviewed"


class ImportCommitted(DomainEvent):
    event_type = "ImportCommitted"


class ExportQueued(DomainEvent):
    event_type = "ExportQueued"


class ExportRan(DomainEvent):
    event_type = "ExportRan"


class MetricDefined(DomainEvent):
    event_type = "MetricDefined"


class OperationalRunStarted(DomainEvent):
    event_type = "OperationalRunStarted"


class OperationalRunFinished(DomainEvent):
    event_type = "OperationalRunFinished"


__all__ = [
    "AttachmentDownloaded",
    "AttachmentScanned",
    "AttachmentUploaded",
    "ExportQueued",
    "ExportRan",
    "ImportCommitted",
    "ImportPreviewed",
    "MetricDefined",
    "OperationalRunFinished",
    "OperationalRunStarted",
]
