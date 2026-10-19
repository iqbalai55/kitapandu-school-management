"""Domain events emitted by the identity context.

Past-tense aggregate events (Round 2 Q12). Each event carries the actor + tenant
context so consumers (notifications, audit projection) can route without re-querying.
"""
from __future__ import annotations

from ksma.shared.events import DomainEvent


class TenantCreated(DomainEvent):
    event_type = "TenantCreated"


class TenantActivated(DomainEvent):
    event_type = "TenantActivated"


class EducationUnitCreated(DomainEvent):
    event_type = "EducationUnitCreated"


class AccountCreated(DomainEvent):
    event_type = "AccountCreated"


class AccountActivated(DomainEvent):
    event_type = "AccountActivated"


class MembershipInvited(DomainEvent):
    event_type = "MembershipInvited"


class MembershipActivated(DomainEvent):
    event_type = "MembershipActivated"


class MembershipRevoked(DomainEvent):
    event_type = "MembershipRevoked"


class AccessAssignmentGranted(DomainEvent):
    event_type = "AccessAssignmentGranted"


class AccessAssignmentRevoked(DomainEvent):
    event_type = "AccessAssignmentRevoked"


class AuthzRevisionChanged(DomainEvent):
    event_type = "AuthzRevisionChanged"


class PasswordChanged(DomainEvent):
    event_type = "PasswordChanged"


class AccountRecoveryStarted(DomainEvent):
    event_type = "AccountRecoveryStarted"


class AccountRecoveryCompleted(DomainEvent):
    event_type = "AccountRecoveryCompleted"


class SupportGrantOpened(DomainEvent):
    event_type = "SupportGrantOpened"


class SupportGrantClosed(DomainEvent):
    event_type = "SupportGrantClosed"


class PolicyPublished(DomainEvent):
    event_type = "PolicyPublished"