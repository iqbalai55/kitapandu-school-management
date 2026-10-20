"""SQLAlchemy 2.x ORM models for the identity context.

Schema matches ``alembic/versions/0002_identity_root.py``. One ORM class per
table; mapping to/from domain entities lives in the repositories.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, TSTZRANGE, UUID as PgUUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


class TenantRow(Base):
    __tablename__ = "tenant"
    id: Mapped[UUID] = mapped_column(PgUUID(as_uuid=True), primary_key=True,
        server_default=text("ksma_uuid()"))
    display_name: Mapped[str] = mapped_column(Text, nullable=False)
    service_status: Mapped[str] = mapped_column(String(32), nullable=False)
    default_timezone: Mapped[str] = mapped_column(String(64), nullable=False)
    active_policy_id: Mapped[UUID | None] = mapped_column(PgUUID(as_uuid=True), nullable=True)
    activated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_by: Mapped[str] = mapped_column(Text, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_by: Mapped[str] = mapped_column(Text, nullable=False)


class EducationUnitRow(Base):
    __tablename__ = "education_unit"
    id: Mapped[UUID] = mapped_column(PgUUID(as_uuid=True), primary_key=True,
        server_default=text("ksma_uuid()"))
    tenant_id: Mapped[UUID] = mapped_column(PgUUID(as_uuid=True), ForeignKey("tenant.id"), nullable=False)
    unit_code: Mapped[str] = mapped_column(String(64), nullable=False)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    institution_type: Mapped[str] = mapped_column(String(64), nullable=False)
    education_levels: Mapped[list[str]] = mapped_column(ARRAY(String), nullable=False, default=list)
    location: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    timezone: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_by: Mapped[str] = mapped_column(Text, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_by: Mapped[str] = mapped_column(Text, nullable=False)

    __table_args__ = (UniqueConstraint("tenant_id", "unit_code"),)


class AccountRow(Base):
    __tablename__ = "account"
    id: Mapped[UUID] = mapped_column(PgUUID(as_uuid=True), primary_key=True)
    auth_subject: Mapped[str] = mapped_column(Text, nullable=False)
    auth_namespace: Mapped[str] = mapped_column(String(64), nullable=False)
    display_name: Mapped[str] = mapped_column(Text, nullable=False)
    verified_contact: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    password_hash: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    authz_revision: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_by: Mapped[str] = mapped_column(Text, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_by: Mapped[str] = mapped_column(Text, nullable=False)

    __table_args__ = (UniqueConstraint("auth_namespace", "auth_subject"),)


class MembershipRow(Base):
    __tablename__ = "membership"
    id: Mapped[UUID] = mapped_column(PgUUID(as_uuid=True), primary_key=True,
        server_default=text("ksma_uuid()"))
    tenant_id: Mapped[UUID] = mapped_column(PgUUID(as_uuid=True), ForeignKey("tenant.id"), nullable=False)
    account_id: Mapped[UUID] = mapped_column(PgUUID(as_uuid=True), ForeignKey("account.id"), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    valid_from: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    valid_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    invitation_ref: Mapped[str | None] = mapped_column(String(120), nullable=True)
    revocation: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_by: Mapped[str] = mapped_column(Text, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_by: Mapped[str] = mapped_column(Text, nullable=False)

    __table_args__ = (UniqueConstraint("tenant_id", "account_id"),)


class RefreshTokenRow(Base):
    __tablename__ = "refresh_token"
    id: Mapped[UUID] = mapped_column(
        PgUUID(as_uuid=True), primary_key=True,
        server_default=text("ksma_uuid()"),
    )
    tenant_id: Mapped[UUID] = mapped_column(PgUUID(as_uuid=True), ForeignKey("tenant.id"), nullable=False)
    account_id: Mapped[UUID] = mapped_column(PgUUID(as_uuid=True), ForeignKey("account.id"), nullable=False)
    membership_id: Mapped[UUID] = mapped_column(PgUUID(as_uuid=True), ForeignKey("membership.id"), nullable=False)
    token_hash: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    issued_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    revoked_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    replaced_by_id: Mapped[UUID | None] = mapped_column(PgUUID(as_uuid=True), nullable=True)


class RecoveryTokenRow(Base):
    __tablename__ = "recovery_token"
    id: Mapped[UUID] = mapped_column(PgUUID(as_uuid=True), primary_key=True,
        server_default=text("ksma_uuid()"))
    account_id: Mapped[UUID] = mapped_column(PgUUID(as_uuid=True), ForeignKey("account.id"), nullable=False)
    token_hash: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    purpose: Mapped[str] = mapped_column(String(32), nullable=False)
    issued_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    consumed_ip: Mapped[str | None] = mapped_column(String(64), nullable=True)


class ServicePrincipalRow(Base):
    __tablename__ = "service_principal"
    id: Mapped[UUID] = mapped_column(PgUUID(as_uuid=True), primary_key=True,
        server_default=text("ksma_uuid()"))
    principal_code: Mapped[str] = mapped_column(String(120), nullable=False, unique=True)
    purpose: Mapped[str] = mapped_column(Text, nullable=False)
    environment: Mapped[str] = mapped_column(String(32), nullable=False)
    credential_ref: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    rotation_due_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class PolicyVersionRow(Base):
    __tablename__ = "policy_version"
    id: Mapped[UUID] = mapped_column(PgUUID(as_uuid=True), primary_key=True,
        server_default=text("ksma_uuid()"))
    tenant_id: Mapped[UUID] = mapped_column(PgUUID(as_uuid=True), ForeignKey("tenant.id"), nullable=False)
    policy_domain: Mapped[str] = mapped_column(String(64), nullable=False)
    unit_scope_id: Mapped[UUID | None] = mapped_column(PgUUID(as_uuid=True), nullable=True)
    program_scope_id: Mapped[UUID | None] = mapped_column(PgUUID(as_uuid=True), nullable=True)
    version_no: Mapped[int] = mapped_column(Integer, nullable=False)
    effective_range: Mapped[Any] = mapped_column(TSTZRANGE, nullable=False)
    ruleset: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    approval: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_by: Mapped[str] = mapped_column(Text, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_by: Mapped[str] = mapped_column(Text, nullable=False)

    __table_args__ = (UniqueConstraint("tenant_id", "policy_domain", "version_no"),)


class AccessAssignmentRow(Base):
    __tablename__ = "access_assignment"
    id: Mapped[UUID] = mapped_column(PgUUID(as_uuid=True), primary_key=True,
        server_default=text("ksma_uuid()"))
    tenant_id: Mapped[UUID] = mapped_column(PgUUID(as_uuid=True), ForeignKey("tenant.id"), nullable=False)
    membership_id: Mapped[UUID | None] = mapped_column(PgUUID(as_uuid=True), ForeignKey("membership.id"), nullable=True)
    service_principal_id: Mapped[UUID | None] = mapped_column(PgUUID(as_uuid=True), ForeignKey("service_principal.id"), nullable=True)
    role_code: Mapped[str] = mapped_column(String(16), nullable=False)
    action_ids: Mapped[list[str]] = mapped_column(ARRAY(String), nullable=False)
    scope: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    valid_from: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    valid_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    policy_id: Mapped[UUID] = mapped_column(PgUUID(as_uuid=True), ForeignKey("policy_version.id"), nullable=False)
    authorization: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    revocation: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_by: Mapped[str] = mapped_column(Text, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_by: Mapped[str] = mapped_column(Text, nullable=False)


class SupportGrantRow(Base):
    __tablename__ = "support_grant"
    id: Mapped[UUID] = mapped_column(PgUUID(as_uuid=True), primary_key=True,
        server_default=text("ksma_uuid()"))
    tenant_id: Mapped[UUID] = mapped_column(PgUUID(as_uuid=True), ForeignKey("tenant.id"), nullable=False)
    operator_account_id: Mapped[UUID] = mapped_column(PgUUID(as_uuid=True), ForeignKey("account.id"), nullable=False)
    ticket_ref: Mapped[str] = mapped_column(String(120), nullable=False)
    scope: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    approved_by: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    valid_from: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    valid_until: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    revocation: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class AuditRecordRow(Base):
    __tablename__ = "audit_record"
    id: Mapped[UUID] = mapped_column(PgUUID(as_uuid=True), primary_key=True,
        server_default=text("ksma_uuid()"))
    tenant_id: Mapped[UUID] = mapped_column(PgUUID(as_uuid=True), nullable=False)
    actor: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    action_id: Mapped[str] = mapped_column(Text, nullable=False)
    object_ref: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    change_ref: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    authorization_ref: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    correlation_id: Mapped[str] = mapped_column(String(64), nullable=False)