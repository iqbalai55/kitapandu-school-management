"""Finance domain events."""
from __future__ import annotations

from ksma.shared.events import DomainEvent


class InvoiceCreated(DomainEvent):
    event_type = "InvoiceCreated"


class InvoiceIssued(DomainEvent):
    event_type = "InvoiceIssued"


class InvoiceVoided(DomainEvent):
    event_type = "InvoiceVoided"


class InvoiceCorrected(DomainEvent):
    event_type = "InvoiceCorrected"


class PaymentSubmitted(DomainEvent):
    event_type = "PaymentSubmitted"


class PaymentVerified(DomainEvent):
    event_type = "PaymentVerified"


class PaymentRejected(DomainEvent):
    event_type = "PaymentRejected"


class FundsAllocated(DomainEvent):
    event_type = "FundsAllocated"


class FundsAvailableChanged(DomainEvent):
    event_type = "FundsAvailableChanged"


class AllocationReversed(DomainEvent):
    event_type = "AllocationReversed"


class PaymentReversed(DomainEvent):
    event_type = "PaymentReversed"


class ReceiptIssued(DomainEvent):
    event_type = "ReceiptIssued"


class ReceiptReplaced(DomainEvent):
    event_type = "ReceiptReplaced"