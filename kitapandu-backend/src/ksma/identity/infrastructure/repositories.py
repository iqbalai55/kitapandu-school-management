"""Identity BC repositories.

Per ADR-0002, this module owns the SQL/repo load pattern for the identity
BC tables (tenant, education_unit, account, membership, access_assignment,
service_principal, support_grant, policy_version, refresh_token,
recovery_token, audit_record).

The full implementations of these repositories belong to ticket #20
(Identity BC comprehensive). For now, this module provides the seam
that ``shared.auth.dependencies`` and ``shared.rbac`` need to import —
the constructors + the few methods used by the request-time resolver.

Seam: pure-Python repository pattern with raw SQL; ``repositories are
constructed with (session, tenant_id)``; methods are async; commits are
left to the caller's session.
"""
from __future__ import annotations

import json
from datetime import date as _date, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession


class TenantRepository:
    def __init__(self, session: AsyncSession, tenant_id: UUID | None = None) -> None:
        self.session = session
        self.tenant_id = tenant_id

    async def get(self, tenant_id: UUID) -> dict[str, Any] | None:
        row = (await self.session.execute(
            text("SELECT id, display_name, service_status, default_timezone, "
                 "active_policy_id, activated_at FROM tenant WHERE id = :id"),
            {"id": str(tenant_id)},
        )).first()
        return dict(row._mapping) if row else None


class EducationUnitRepository:
    def __init__(self, session: AsyncSession, tenant_id: UUID | None = None) -> None:
        self.session = session
        self.tenant_id = tenant_id

    async def get(self, unit_id: UUID) -> dict[str, Any] | None:
        row = (await self.session.execute(
            text("SELECT id, tenant_id, unit_code, name, institution_type, "
                 "education_levels, status, timezone FROM education_unit WHERE id = :id"),
            {"id": str(unit_id)},
        )).first()
        return dict(row._mapping) if row else None


class AccountRepository:
    def __init__(self, session: AsyncSession, tenant_id: UUID | None = None) -> None:
        self.session = session
        self.tenant_id = tenant_id

    async def get_by_id(self, account_id: UUID) -> dict[str, Any] | None:
        row = (await self.session.execute(
            text(
                "SELECT id, auth_subject, auth_namespace, display_name, status, "
                "authz_revision, password_hash FROM account WHERE id = :id"
            ),
            {"id": str(account_id)},
        )).first()
        return dict(row._mapping) if row else None


class MembershipRepository:
    def __init__(self, session: AsyncSession, tenant_id: UUID | None = None) -> None:
        self.session = session
        self.tenant_id = tenant_id

    async def get_for_account(
        self, account_id: UUID, tenant_id: UUID
    ) -> dict[str, Any] | None:
        row = (await self.session.execute(
            text(
                "SELECT id, tenant_id, account_id, status, valid_from, valid_until "
                "FROM membership WHERE account_id = :aid AND tenant_id = :tid "
                "AND status = 'active' LIMIT 1"
            ),
            {"aid": str(account_id), "tid": str(tenant_id)},
        )).first()
        return dict(row._mapping) if row else None


class AccessAssignmentRepository:
    def __init__(self, session: AsyncSession, tenant_id: UUID | None = None) -> None:
        self.session = session
        self.tenant_id = tenant_id

    async def list_active_for_membership(
        self, membership_id: UUID, *, now: datetime | None = None
    ) -> list[dict[str, Any]]:
        """List the active grants for a membership at time ``now``.

        Returns dicts with the columns the auth dependency / RBAC resolver
        need: ``id, role_code, action_ids, scope, valid_from, valid_until,
        status, policy_id``.
        """
        ts = (now or datetime.utcnow()).isoformat()
        rows = (await self.session.execute(
            text(
                """
                SELECT id, tenant_id, membership_id, role_code, action_ids, scope,
                       valid_from, valid_until, status, policy_id, authorization
                FROM access_assignment
                WHERE membership_id = :mid
                  AND status = 'active'
                  AND valid_from <= CAST(:ts AS timestamptz)
                  AND (valid_until IS NULL OR valid_until > CAST(:ts AS timestamptz))
                """
            ),
            {"mid": str(membership_id), "ts": ts},
        )).all()
        return [dict(r._mapping) for r in rows]


class RefreshTokenRepository:
    def __init__(self, session: AsyncSession, tenant_id: UUID | None = None) -> None:
        self.session = session
        self.tenant_id = tenant_id

    async def get_by_hash(self, token_hash: str) -> dict[str, Any] | None:
        row = (await self.session.execute(
            text("SELECT id, account_id, expires_at, revoked_at, revoked_reason, "
                 "replaced_by_id FROM refresh_token WHERE token_hash = :h LIMIT 1"),
            {"h": token_hash},
        )).first()
        return dict(row._mapping) if row else None


class RecoveryTokenRepository:
    def __init__(self, session: AsyncSession, tenant_id: UUID | None = None) -> None:
        self.session = session
        self.tenant_id = tenant_id


class ServicePrincipalRepository:
    def __init__(self, session: AsyncSession, tenant_id: UUID | None = None) -> None:
        self.session = session
        self.tenant_id = tenant_id


class PolicyVersionRepository:
    def __init__(self, session: AsyncSession, tenant_id: UUID | None = None) -> None:
        self.session = session
        self.tenant_id = tenant_id


class SupportGrantRepository:
    def __init__(self, session: AsyncSession, tenant_id: UUID | None = None) -> None:
        self.session = session
        self.tenant_id = tenant_id


class AuditRepository:
    def __init__(self, session: AsyncSession, tenant_id: UUID | None = None) -> None:
        self.session = session
        self.tenant_id = tenant_id


__all__ = [
    "TenantRepository",
    "EducationUnitRepository",
    "AccountRepository",
    "MembershipRepository",
    "AccessAssignmentRepository",
    "RefreshTokenRepository",
    "RecoveryTokenRepository",
    "ServicePrincipalRepository",
    "PolicyVersionRepository",
    "SupportGrantRepository",
    "AuditRepository",
]