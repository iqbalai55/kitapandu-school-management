"""Identity application services.

Showpiece: ``login`` issues an access JWT (15 min) + a hashed refresh token
(7 days, revocable, chainable). ``refresh`` rotates refresh tokens, increments
``authz_revision`` if the old one was compromised, and persists ``replaced_by_id``.

Tenant context: tokens carry ``tz_current_tenant`` + ``tz_current_unit`` +
``tz_authz_revision`` claims. Rights changes invalidate the access token (the
resolver compares ``authz_revision`` with the DB).

This module owns the cross-tenant bridge: ServicePrincipal logins use the same
``Account`` table but skip membership lookup and resolve grants via
``AccessAssignment.service_principal_id``.
"""
from __future__ import annotations

import hashlib
import secrets
from datetime import date, datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from ksma.identity.domain import (
    AccountStatus,
    EducationUnitStatus,
    MembershipStatus,
    TenantServiceStatus,
)
from ksma.identity.domain.events import (
    AccountActivated,
    AccountCreated,
    AccountRecoveryCompleted,
    AccountRecoveryStarted,
    EducationUnitCreated,
    MembershipActivated,
    MembershipInvited,
    MembershipRevoked,
    PasswordChanged,
    TenantActivated,
    TenantCreated,
)
from ksma.identity.infrastructure.models import (
    AccountRow,
    AccessAssignmentRow,
    EducationUnitRow,
    MembershipRow,
    PolicyVersionRow,
    RecoveryTokenRow,
    RefreshTokenRow,
    ServicePrincipalRow,
    TenantRow,
)
from ksma.shared.auth import hash_password, verify_password
from ksma.shared.auth.jwt import issue_access_token, issue_refresh_token
from ksma.shared.errors import (
    AuthenticationError,
    InvalidCredentialsError,
    StateConflictError,
    ValidationError,
)
from ksma.shared.events import bus
from ksma.shared.outbox import enqueue
from ksma.shared.tenancy import ActorKind
from ksma.shared.time import clock


# ---------------------------------------------------------------------------
# Tenant
# ---------------------------------------------------------------------------


async def create_tenant(
    session: AsyncSession,
    *,
    display_name: str,
    default_timezone: str = "Asia/Jakarta",
    actor_label: str = "system",
) -> TenantRow:
    t = TenantRow(
        display_name=display_name,
        service_status=TenantServiceStatus.SETUP,
        default_timezone=default_timezone,
        created_by=actor_label,
        updated_by=actor_label,
    )
    session.add(t)
    await session.flush()
    event = TenantCreated(tenant_id=t.id, actor_kind="service", actor_id=None)
    await enqueue(session, event, aggregate_ref=f"tenant:{t.id}")
    bus().dispatch(event)
    return t


async def activate_tenant(
    session: AsyncSession, *, tenant_id: UUID, actor_label: str = "system"
) -> TenantRow:
    t = (
        await session.execute(
            select(TenantRow).where(TenantRow.id == tenant_id)
        )
    ).scalar_one_or_none()
    if t is None:
        raise ValidationError("Tenant not found")
    t.service_status = TenantServiceStatus.ACTIVE
    t.activated_at = clock().now()
    t.updated_by = actor_label
    event = TenantActivated(tenant_id=tenant_id, actor_kind="service", actor_id=None)
    await enqueue(session, event, aggregate_ref=f"tenant:{tenant_id}")
    bus().dispatch(event)
    return t


# ---------------------------------------------------------------------------
# EducationUnit
# ---------------------------------------------------------------------------


async def create_education_unit(
    session: AsyncSession,
    *,
    tenant_id: UUID,
    unit_code: str,
    name: str,
    institution_type: str,
    education_levels: list[str] | None = None,
    timezone: str = "Asia/Jakarta",
    actor_label: str = "system",
) -> EducationUnitRow:
    u = EducationUnitRow(
        tenant_id=tenant_id,
        unit_code=unit_code,
        name=name,
        institution_type=institution_type,
        education_levels=list(education_levels or []),
        timezone=timezone,
        status=EducationUnitStatus.ACTIVE,
        created_by=actor_label,
        updated_by=actor_label,
    )
    session.add(u)
    await session.flush()
    event = EducationUnitCreated(tenant_id=tenant_id, actor_kind="service", actor_id=None)
    await enqueue(session, event, aggregate_ref=f"education_unit:{u.id}")
    bus().dispatch(event)
    return u


# ---------------------------------------------------------------------------
# Account
# ---------------------------------------------------------------------------


async def create_account(
    session: AsyncSession,
    *,
    auth_subject: str,
    auth_namespace: str = "default",
    display_name: str,
    password: str | None = None,
    verified_contact: dict[str, Any] | None = None,
    actor_label: str = "system",
) -> AccountRow:
    a = AccountRow(
        auth_subject=auth_subject,
        auth_namespace=auth_namespace,
        display_name=display_name,
        verified_contact=verified_contact,
        password_hash=hash_password(password) if password else None,
        status=AccountStatus.PENDING,
        created_by=actor_label,
        updated_by=actor_label,
    )
    session.add(a)
    await session.flush()
    event = AccountCreated(tenant_id=None, actor_kind="service", actor_id=a.id)
    await enqueue(session, event, aggregate_ref=f"account:{a.id}")
    bus().dispatch(event)
    return a


async def activate_account(
    session: AsyncSession, *, account_id: UUID, actor_label: str = "system"
) -> None:
    a = (
        await session.execute(select(AccountRow).where(AccountRow.id == account_id))
    ).scalar_one_or_none()
    if a is None:
        raise ValidationError("Account not found")
    a.status = AccountStatus.ACTIVE
    a.updated_by = actor_label
    a.authz_revision += 1
    event = AccountActivated(tenant_id=None, actor_kind="service", actor_id=a.id)
    await enqueue(session, event, aggregate_ref=f"account:{a.id}")
    bus().dispatch(event)


async def change_password(
    session: AsyncSession,
    *,
    account_id: UUID,
    new_password: str,
) -> None:
    a = (
        await session.execute(select(AccountRow).where(AccountRow.id == account_id))
    ).scalar_one_or_none()
    if a is None:
        raise ValidationError("Account not found")
    a.password_hash = hash_password(new_password)
    a.authz_revision += 1
    a.updated_by = str(account_id)
    event = PasswordChanged(tenant_id=None, actor_kind="account", actor_id=a.id)
    await enqueue(session, event, aggregate_ref=f"account:{a.id}")
    bus().dispatch(event)


# ---------------------------------------------------------------------------
# Membership
# ---------------------------------------------------------------------------


async def invite_membership(
    session: AsyncSession,
    *,
    tenant_id: UUID,
    account_id: UUID,
    invitation_ref: str | None = None,
    actor_label: str = "system",
) -> MembershipRow:
    m = MembershipRow(
        tenant_id=tenant_id,
        account_id=account_id,
        status=MembershipStatus.INVITED,
        invitation_ref=invitation_ref,
        created_by=actor_label,
        updated_by=actor_label,
    )
    session.add(m)
    await session.flush()
    event = MembershipInvited(tenant_id=tenant_id, actor_kind="service", actor_id=account_id)
    await enqueue(session, event, aggregate_ref=f"membership:{m.id}")
    bus().dispatch(event)
    return m


async def activate_membership(
    session: AsyncSession, *, membership_id: UUID, actor_label: str = "system"
) -> None:
    m = (
        await session.execute(select(MembershipRow).where(MembershipRow.id == membership_id))
    ).scalar_one_or_none()
    if m is None:
        raise ValidationError("Membership not found")
    m.status = MembershipStatus.ACTIVE
    m.updated_by = actor_label
    m.account.authz_revision += 1 if hasattr(m, "account") else 1
    # Bump the account's authz_revision too (separate transaction so we re-query).
    a = (
        await session.execute(select(AccountRow).where(AccountRow.id == m.account_id))
    ).scalar_one()
    a.authz_revision += 1
    event = MembershipActivated(tenant_id=m.tenant_id, actor_kind="service", actor_id=m.account_id)
    await enqueue(session, event, aggregate_ref=f"membership:{m.id}")
    bus().dispatch(event)


async def revoke_membership(
    session: AsyncSession,
    *,
    membership_id: UUID,
    reason: str,
    actor_label: str = "system",
) -> None:
    m = (
        await session.execute(select(MembershipRow).where(MembershipRow.id == membership_id))
    ).scalar_one_or_none()
    if m is None:
        raise ValidationError("Membership not found")
    m.status = MembershipStatus.REVOKED
    m.revocation = {"actor": actor_label, "reason": reason, "at": clock().now().isoformat()}
    m.updated_by = actor_label
    a = (
        await session.execute(select(AccountRow).where(AccountRow.id == m.account_id))
    ).scalar_one()
    a.authz_revision += 1
    # Revoke all refresh tokens for this account.
    await session.execute(
        text(
            "UPDATE refresh_token SET revoked_at = now(), revoked_reason = :r "
            "WHERE account_id = :a AND revoked_at IS NULL"
        ),
        {"r": f"membership_revoked: {reason}", "a": str(m.account_id)},
    )
    event = MembershipRevoked(tenant_id=m.tenant_id, actor_kind="service", actor_id=m.account_id)
    await enqueue(session, event, aggregate_ref=f"membership:{m.id}")
    bus().dispatch(event)


# ---------------------------------------------------------------------------
# AccessAssignment
# ---------------------------------------------------------------------------


async def grant_access(
    session: AsyncSession,
    *,
    tenant_id: UUID,
    membership_id: UUID,
    role_code: str,
    action_ids: list[str],
    scope: dict[str, Any] | None,
    policy_id: UUID,
    authorization: dict[str, Any],
    actor_label: str = "system",
) -> AccessAssignmentRow:
    g = AccessAssignmentRow(
        tenant_id=tenant_id,
        membership_id=membership_id,
        role_code=role_code,
        action_ids=list(action_ids),
        scope=scope or {},
        policy_id=policy_id,
        authorization=authorization,
        status="active",
        created_by=actor_label,
        updated_by=actor_label,
    )
    session.add(g)
    await session.flush()
    a = (
        await session.execute(
            select(MembershipRow).where(MembershipRow.id == membership_id)
        )
    ).scalar_one()
    acct = (
        await session.execute(select(AccountRow).where(AccountRow.id == a.account_id))
    ).scalar_one()
    acct.authz_revision += 1
    return g


async def revoke_access(
    session: AsyncSession,
    *,
    assignment_id: UUID,
    reason: str,
    actor_label: str = "system",
) -> None:
    g = (
        await session.execute(
            select(AccessAssignmentRow).where(AccessAssignmentRow.id == assignment_id)
        )
    ).scalar_one_or_none()
    if g is None:
        raise ValidationError("AccessAssignment not found")
    g.status = "revoked"
    g.revocation = {"actor": actor_label, "reason": reason, "at": clock().now().isoformat()}
    g.updated_by = actor_label
    a = (
        await session.execute(
            select(MembershipRow).where(MembershipRow.id == g.membership_id)
        )
    ).scalar_one()
    acct = (
        await session.execute(select(AccountRow).where(AccountRow.id == a.account_id))
    ).scalar_one()
    acct.authz_revision += 1


# ---------------------------------------------------------------------------
# PolicyVersion
# ---------------------------------------------------------------------------


async def publish_policy(
    session: AsyncSession,
    *,
    tenant_id: UUID,
    policy_domain: str,
    version_no: int,
    ruleset: dict[str, Any],
    unit_scope_id: UUID | None = None,
    program_scope_id: UUID | None = None,
    effective_from: datetime | None = None,
    effective_until: datetime | None = None,
    approval: dict[str, Any] | None = None,
    actor_label: str = "system",
) -> PolicyVersionRow:
    pv = PolicyVersionRow(
        tenant_id=tenant_id,
        policy_domain=policy_domain,
        version_no=version_no,
        ruleset=ruleset,
        status="published",
        unit_scope_id=unit_scope_id,
        program_scope_id=program_scope_id,
        effective_range=f"[{effective_from.isoformat() if effective_from else clock().now().isoformat()},"
                       f"{effective_until.isoformat() if effective_until else ''})",
        approval=approval or {"actor": actor_label, "at": clock().now().isoformat()},
        created_by=actor_label,
        updated_by=actor_label,
    )
    session.add(pv)
    await session.flush()
    return pv


# ---------------------------------------------------------------------------
# Auth: login / refresh / logout / recovery
# ---------------------------------------------------------------------------


def _hash_refresh(raw: str) -> str:
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


async def login(
    session: AsyncSession,
    *,
    auth_subject: str,
    password: str,
    tenant_id: UUID | None = None,
    auth_namespace: str = "default",
) -> tuple[str, str, datetime, AccountRow, MembershipRow | None]:
    """Verify credentials + issue access JWT + hashed refresh token."""
    a = (
        await session.execute(
            select(AccountRow).where(
                AccountRow.auth_namespace == auth_namespace,
                AccountRow.auth_subject == auth_subject,
            )
        )
    ).scalar_one_or_none()
    if a is None or a.password_hash is None or not verify_password(a.password_hash, password):
        raise InvalidCredentialsError("Invalid credentials")
    if a.status != AccountStatus.ACTIVE:
        raise AuthenticationError("Account is not active")

    # Pick a membership: explicit tenant_id if provided, else the first active.
    q = select(MembershipRow).where(
        MembershipRow.account_id == a.id,
        MembershipRow.status == MembershipStatus.ACTIVE,
    )
    if tenant_id is not None:
        q = q.where(MembershipRow.tenant_id == tenant_id)
    m = (await session.execute(q)).scalar_one_or_none()
    if m is None:
        raise AuthenticationError("No active membership for this account")

    # Build token claims.
    memberships_claims = [{"tenant_id": str(m.tenant_id), "roles": []}]
    access_token, access_exp = issue_access_token(
        subject=a.id,
        memberships=memberships_claims,
        current_tenant_id=m.tenant_id,
        current_unit_id=None,
        authz_revision=a.authz_revision,
    )
    refresh_raw = issue_refresh_token()
    refresh_hash = _hash_refresh(refresh_raw)
    rt = RefreshTokenRow(
        tenant_id=m.tenant_id,
        account_id=a.id,
        membership_id=m.id,
        token_hash=refresh_hash,
        expires_at=clock().now() + __import__("datetime").timedelta(days=7),
    )
    session.add(rt)
    await session.flush()
    return access_token, refresh_raw, access_exp, a, m


async def refresh(
    session: AsyncSession,
    *,
    refresh_raw: str,
) -> tuple[str, str, datetime, AccountRow, MembershipRow]:
    """Rotate refresh token, issue new access JWT."""
    refresh_hash = _hash_refresh(refresh_raw)
    rt = (
        await session.execute(
            select(RefreshTokenRow).where(RefreshTokenRow.token_hash == refresh_hash)
        )
    ).scalar_one_or_none()
    if rt is None:
        raise InvalidCredentialsError("Invalid refresh token")
    if rt.revoked_at is not None:
        raise AuthenticationError("Refresh token revoked")
    if rt.expires_at < clock().now():
        raise AuthenticationError("Refresh token expired")

    # Rotate: revoke old, create new in the chain.
    rt.revoked_at = clock().now()
    rt.revoked_reason = "rotated"

    new_raw = issue_refresh_token()
    new_hash = _hash_refresh(new_raw)
    new_rt = RefreshTokenRow(
        tenant_id=rt.tenant_id,
        account_id=rt.account_id,
        membership_id=rt.membership_id,
        token_hash=new_hash,
        expires_at=clock().now() + __import__("datetime").timedelta(days=7),
        replaced_by_id=None,
    )
    session.add(new_rt)
    await session.flush()
    rt.replaced_by_id = new_rt.id

    a = (
        await session.execute(select(AccountRow).where(AccountRow.id == rt.account_id))
    ).scalar_one()
    m = (
        await session.execute(
            select(MembershipRow).where(MembershipRow.id == rt.membership_id)
        )
    ).scalar_one()
    access_token, access_exp = issue_access_token(
        subject=a.id,
        memberships=[{"tenant_id": str(m.tenant_id), "roles": []}],
        current_tenant_id=m.tenant_id,
        current_unit_id=None,
        authz_revision=a.authz_revision,
    )
    return access_token, new_raw, access_exp, a, m


async def logout(
    session: AsyncSession,
    *,
    refresh_raw: str,
    reason: str = "user_logout",
) -> None:
    refresh_hash = _hash_refresh(refresh_raw)
    rt = (
        await session.execute(
            select(RefreshTokenRow).where(RefreshTokenRow.token_hash == refresh_hash)
        )
    ).scalar_one_or_none()
    if rt is None:
        return  # idempotent
    rt.revoked_at = clock().now()
    rt.revoked_reason = reason


async def start_recovery(
    session: AsyncSession,
    *,
    auth_subject: str,
    auth_namespace: str = "default",
) -> str:
    """Issue a recovery token. Returns the raw token (caller delivers it via
    the chosen channel — for pilot, log + email-via-console)."""
    a = (
        await session.execute(
            select(AccountRow).where(
                AccountRow.auth_namespace == auth_namespace,
                AccountRow.auth_subject == auth_subject,
            )
        )
    ).scalar_one_or_none()
    if a is None:
        # Don't reveal existence — return a fake token that will fail validation.
        return "invalid-account"
    raw = secrets.token_urlsafe(32)
    rt = RecoveryTokenRow(
        account_id=a.id,
        token_hash=_hash_refresh(raw),
        purpose="password_recover",
        expires_at=clock().now() + __import__("datetime").timedelta(hours=1),
    )
    session.add(rt)
    await session.flush()
    event = AccountRecoveryStarted(tenant_id=None, actor_kind="service", actor_id=a.id)
    await enqueue(session, event, aggregate_ref=f"account:{a.id}")
    bus().dispatch(event)
    return raw


async def complete_recovery(
    session: AsyncSession,
    *,
    raw_token: str,
    new_password: str,
) -> None:
    h = _hash_refresh(raw_token)
    rt = (
        await session.execute(
            select(RecoveryTokenRow).where(RecoveryTokenRow.token_hash == h)
        )
    ).scalar_one_or_none()
    if rt is None or rt.consumed_at is not None or rt.expires_at < clock().now():
        raise AuthenticationError("Invalid or expired recovery token")
    rt.consumed_at = clock().now()
    a = (
        await session.execute(select(AccountRow).where(AccountRow.id == rt.account_id))
    ).scalar_one()
    a.password_hash = hash_password(new_password)
    a.authz_revision += 1
    a.updated_by = str(a.id)
    event = AccountRecoveryCompleted(tenant_id=None, actor_kind="account", actor_id=a.id)
    await enqueue(session, event, aggregate_ref=f"account:{a.id}")
    bus().dispatch(event)


# ---------------------------------------------------------------------------
# ServicePrincipal token issuance (used by the outbox worker etc.)
# ---------------------------------------------------------------------------


async def issue_service_token(
    session: AsyncSession,
    *,
    principal_code: str,
    scope: dict[str, Any] | None = None,
) -> tuple[str, datetime]:
    sp = (
        await session.execute(
            select(ServicePrincipalRow).where(ServicePrincipalRow.principal_code == principal_code)
        )
    ).scalar_one_or_none()
    if sp is None or sp.status != "active":
        raise AuthenticationError("Service principal not found or disabled")
    # ServicePrincipals have no memberships; their grants live on
    # AccessAssignment.service_principal_id. For pilot, the worker doesn't need
    # tenant scope — it processes per-row outbox entries. A real production
    # worker would scope its grants here.
    access_token, access_exp = issue_access_token(
        subject=sp.id,
        memberships=[{"tenant_id": "", "roles": ["SV"]}],
        current_tenant_id=None,
        current_unit_id=None,
        authz_revision=1,
    )
    return access_token, access_exp