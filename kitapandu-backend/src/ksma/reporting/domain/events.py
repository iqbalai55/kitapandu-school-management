"""Reporting domain events."""
from __future__ import annotations

from ksma.shared.events import DomainEvent


class ReportDrafted(DomainEvent):
    event_type = "ReportDrafted"


class ReportPublished(DomainEvent):
    event_type = "ReportPublished"


class ReportSuperseded(DomainEvent):
    event_type = "ReportSuperseded"


class ReportRevoked(DomainEvent):
    event_type = "ReportRevoked"


class CompletionDecided(DomainEvent):
    event_type = "CompletionDecided"


class CertificateIssued(DomainEvent):
    event_type = "CertificateIssued"


class CertificateReplaced(DomainEvent):
    event_type = "CertificateReplaced"


class CertificateRevoked(DomainEvent):
    event_type = "CertificateRevoked"