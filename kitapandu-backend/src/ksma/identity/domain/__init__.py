"""Identity domain entities + value objects.

Pure dataclasses; no SQLAlchemy imports here. Mapping to ORM models lives in
``ksma.identity.infrastructure``.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any
from uuid import UUID, uuid4


class TenantServiceStatus(StrEnum):
    SETUP = "setup"
    ACTIVE = "active"
    SUSPENDED = "suspended"


class EducationUnitStatus(StrEnum):
    ACTIVE = "active"
    INACTIVE = "inactive"


class AccountStatus(StrEnum):
    PENDING = "pending"
    ACTIVE = "active"
    DISABLED = "disabled"


class MembershipStatus(StrEnum):
    INVITED = "invited"
    ACTIVE = "active"
    SUSPENDED = "suspended"
    REVOKED = "revoked"


class PolicyDomain(StrEnum):
    ACCESS = "access"
    ENROLLMENT = "enrollment"
    SESSION = "session"
    ASSESSMENT = "assessment"
    FINANCE = "finance"
    COMMUNICATION = "communication"
    RETENTION = "retention"
    CERTIFICATE = "certificate"


class PolicyStatus(StrEnum):
    DRAFT = "draft"
    PUBLISHED = "published"
    RETIRED = "retired"


class ServicePrincipalEnvironment(StrEnum):
    DEVELOPMENT = "development"
    TEST = "test"
    PRODUCTION = "production"


@dataclass(slots=True)
class Tenant:
    """Root tenant (ROOT scope).

    ``id`` is the tenant identity; every T/U row carries ``tenant_id`` and RLS
    policies use it. ``display_name`` is NOT a unique key — two tenants can
    share a name.
    """

    id: UUID = field(default_factory=uuid4)
    display_name: str = ""
    service_status: TenantServiceStatus = TenantServiceStatus.SETUP
    default_timezone: str = "Asia/Jakarta"
    active_policy_id: UUID | None = None
    activated_at: datetime | None = None
    created_at: datetime | None = None
    created_by: str = ""
    updated_at: datetime | None = None
    updated_by: str = ""


@dataclass(slots=True)
class EducationUnit:
    id: UUID = field(default_factory=uuid4)
    tenant_id: UUID = field(default_factory=uuid4)
    unit_code: str = ""
    name: str = ""
    institution_type: str = ""
    education_levels: list[str] = field(default_factory=list)
    location: dict[str, Any] | None = None
    status: EducationUnitStatus = EducationUnitStatus.ACTIVE
    timezone: str = "Asia/Jakarta"
    created_at: datetime | None = None
    created_by: str = ""
    updated_at: datetime | None = None
    updated_by: str = ""


@dataclass(slots=True)
class Account:
    """Global login identity (G scope). Does NOT auto-unify participant profiles."""

    id: UUID = field(default_factory=uuid4)
    auth_subject: str = ""
    auth_namespace: str = "default"
    display_name: str = ""
    verified_contact: dict[str, Any] | None = None
    password_hash: str | None = None
    status: AccountStatus = AccountStatus.PENDING
    authz_revision: int = 1
    created_at: datetime | None = None
    created_by: str = ""
    updated_at: datetime | None = None
    updated_by: str = ""

    def bump_authz_revision(self) -> None:
        """Increment on any rights change; access JWTs with stale revision are rejected."""
        self.authz_revision += 1


@dataclass(slots=True)
class Membership:
    id: UUID = field(default_factory=uuid4)
    tenant_id: UUID = field(default_factory=uuid4)
    account_id: UUID = field(default_factory=uuid4)
    status: MembershipStatus = MembershipStatus.INVITED
    valid_from: datetime | None = None
    valid_until: datetime | None = None
    invitation_ref: str | None = None
    revocation: dict[str, Any] | None = None
    created_at: datetime | None = None
    created_by: str = ""
    updated_at: datetime | None = None
    updated_by: str = ""


@dataclass(slots=True)
class RefreshToken:
    id: UUID = field(default_factory=uuid4)
    tenant_id: UUID = field(default_factory=uuid4)
    account_id: UUID = field(default_factory=uuid4)
    membership_id: UUID = field(default_factory=uuid4)
    token_hash: str = ""
    issued_at: datetime | None = None
    expires_at: datetime | None = None
    revoked_at: datetime | None = None
    revoked_reason: str | None = None
    replaced_by_id: UUID | None = None


@dataclass(slots=True)
class RecoveryToken:
    id: UUID = field(default_factory=uuid4)
    account_id: UUID = field(default_factory=uuid4)
    token_hash: str = ""
    purpose: str = "password_recover"  # 'password_recover' | 'account_activate'
    issued_at: datetime | None = None
    expires_at: datetime | None = None
    consumed_at: datetime | None = None
    consumed_ip: str | None = None


@dataclass(slots=True)
class ServicePrincipal:
    id: UUID = field(default_factory=uuid4)
    principal_code: str = ""
    purpose: str = ""
    environment: ServicePrincipalEnvironment = ServicePrincipalEnvironment.PRODUCTION
    credential_ref: str = ""
    status: str = "active"
    rotation_due_at: datetime | None = None
    created_at: datetime | None = None


@dataclass(slots=True)
class PolicyVersion:
    id: UUID = field(default_factory=uuid4)
    tenant_id: UUID = field(default_factory=uuid4)
    policy_domain: PolicyDomain = PolicyDomain.ACCESS
    unit_scope_id: UUID | None = None
    program_scope_id: UUID | None = None
    version_no: int = 1
    effective_range: tuple[datetime | None, datetime | None] | None = None
    ruleset: dict[str, Any] = field(default_factory=dict)
    approval: dict[str, Any] | None = None
    status: PolicyStatus = PolicyStatus.DRAFT
    created_at: datetime | None = None
    created_by: str = ""
    updated_at: datetime | None = None
    updated_by: str = ""


@dataclass(slots=True)
class AccessAssignment:
    """One grant. R02 evaluates this as a whole — no splicing."""

    id: UUID = field(default_factory=uuid4)
    tenant_id: UUID = field(default_factory=uuid4)
    membership_id: UUID | None = None
    service_principal_id: UUID | None = None
    role_code: str = ""
    action_ids: list[str] = field(default_factory=list)
    scope: dict[str, Any] = field(default_factory=dict)
    valid_from: datetime | None = None
    valid_until: datetime | None = None
    status: str = "pending"
    policy_id: UUID = field(default_factory=uuid4)
    authorization: dict[str, Any] = field(default_factory=dict)
    revocation: dict[str, Any] | None = None
    created_at: datetime | None = None
    created_by: str = ""
    updated_at: datetime | None = None
    updated_by: str = ""


@dataclass(slots=True)
class SupportGrant:
    """Time-bounded platform-assistance grant on customer content."""

    id: UUID = field(default_factory=uuid4)
    tenant_id: UUID = field(default_factory=uuid4)
    operator_account_id: UUID = field(default_factory=uuid4)
    ticket_ref: str = ""
    scope: dict[str, Any] = field(default_factory=dict)
    approved_by: dict[str, Any] = field(default_factory=dict)
    valid_from: datetime | None = None
    valid_until: datetime | None = None
    status: str = "pending"
    revocation: dict[str, Any] | None = None
    created_at: datetime | None = None


@dataclass(slots=True)
class AuditRecord:
    """Append-only trail. Not edited through operational paths."""

    id: UUID = field(default_factory=uuid4)
    tenant_id: UUID = field(default_factory=uuid4)
    actor: dict[str, Any] = field(default_factory=dict)
    action_id: str = ""
    object_ref: dict[str, Any] = field(default_factory=dict)
    occurred_at: datetime | None = None
    change_ref: dict[str, Any] | None = None
    reason: str | None = None
    authorization_ref: dict[str, Any] = field(default_factory=dict)
    correlation_id: str = ""