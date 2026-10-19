"""Identity FastAPI router — auth, tenants, units, accounts, memberships, grants, policies, audit."""
from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field

from ksma.identity.application import (
    activate_account,
    activate_membership,
    activate_tenant,
    change_password,
    complete_recovery,
    create_account,
    create_education_unit,
    create_tenant,
    grant_access,
    invite_membership,
    login,
    logout,
    publish_policy,
    refresh as refresh_service,
    revoke_access,
    revoke_membership,
    start_recovery,
)
from ksma.identity.domain import (
    AccountStatus,
    EducationUnitStatus,
    MembershipStatus,
    TenantServiceStatus,
)
from ksma.identity.infrastructure.models import (
    AccessAssignmentRow,
    AccountRow,
    AuditRecordRow,
    EducationUnitRow,
    MembershipRow,
    PolicyVersionRow,
    TenantRow,
)
from ksma.shared.auth.dependencies import require_action, tenant_context_dep
from ksma.shared.errors import NotImplementedYetError
from ksma.shared.persistence import session_scope
from ksma.shared.rbac import ActionCode
from ksma.shared.tenancy import TenantContext

router = APIRouter(prefix="/api/v1", tags=["identity"])


# --- Auth schemas -----------------------------------------------------------


class LoginBody(BaseModel):
    auth_subject: str
    password: str
    tenant_id: UUID | None = None


class TokenPair(BaseModel):
    access_token: str
    access_expires_at: datetime
    refresh_token: str


class RefreshBody(BaseModel):
    refresh_token: str


class RecoverStartBody(BaseModel):
    auth_subject: str


class RecoverStartOut(BaseModel):
    # In pilot we return the token directly (no real email); Q09 will move it out-of-band.
    recovery_token: str


class RecoverCompleteBody(BaseModel):
    recovery_token: str
    new_password: str


class ChangePasswordBody(BaseModel):
    new_password: str


# --- Tenant / Unit / Membership schemas --------------------------------------


class TenantCreateBody(BaseModel):
    display_name: str = Field(min_length=1, max_length=200)
    default_timezone: str = "Asia/Jakarta"


class TenantOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    display_name: str
    service_status: str
    default_timezone: str


class EducationUnitCreateBody(BaseModel):
    unit_code: str = Field(min_length=1, max_length=40)
    name: str = Field(min_length=1, max_length=200)
    institution_type: str
    education_levels: list[str] = Field(default_factory=list)
    timezone: str = "Asia/Jakarta"


class EducationUnitOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    unit_code: str
    name: str
    institution_type: str
    education_levels: list[str]
    status: str
    timezone: str


class AccountCreateBody(BaseModel):
    auth_subject: str = Field(min_length=1, max_length=200)
    auth_namespace: str = "default"
    display_name: str = Field(min_length=1, max_length=200)
    password: str | None = None


class AccountOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    auth_subject: str
    display_name: str
    status: str
    authz_revision: int


class InviteBody(BaseModel):
    account_id: UUID
    invitation_ref: str | None = None


class MembershipOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    tenant_id: UUID
    account_id: UUID
    status: str
    valid_from: datetime | None = None
    valid_until: datetime | None = None


class GrantBody(BaseModel):
    membership_id: UUID
    role_code: str
    action_ids: list[str] = Field(min_length=1)
    scope: dict[str, Any] | None = None
    policy_id: UUID
    authorization: dict[str, Any] = Field(default_factory=dict)


class GrantOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    membership_id: UUID | None = None
    service_principal_id: UUID | None = None
    role_code: str
    action_ids: list[str]
    scope: dict[str, Any]
    status: str


class PolicyBody(BaseModel):
    policy_domain: str
    version_no: int = Field(ge=1)
    ruleset: dict[str, Any]
    unit_scope_id: UUID | None = None
    program_scope_id: UUID | None = None
    effective_from: datetime | None = None
    effective_until: datetime | None = None


class PolicyOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    policy_domain: str
    version_no: int
    status: str
    ruleset: dict[str, Any]


class AuditOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    actor: dict[str, Any]
    action_id: str
    object_ref: dict[str, Any]
    occurred_at: datetime
    reason: str | None = None


# --- Auth endpoints ---------------------------------------------------------


@router.post("/auth/login", response_model=TokenPair)
async def post_login(body: LoginBody) -> TokenPair:
    async with session_scope() as session:
        access, refresh, exp, _a, _m = await login(
            session,
            auth_subject=body.auth_subject,
            password=body.password,
            tenant_id=body.tenant_id,
        )
        return TokenPair(
            access_token=access,
            access_expires_at=exp,
            refresh_token=refresh,
        )


@router.post("/auth/refresh", response_model=TokenPair)
async def post_refresh(body: RefreshBody) -> TokenPair:
    async with session_scope() as session:
        access, refresh, exp, _a, _m = await refresh_service(session, refresh_raw=body.refresh_token)
        return TokenPair(
            access_token=access,
            access_expires_at=exp,
            refresh_token=refresh,
        )


@router.post("/auth/logout", status_code=status.HTTP_204_NO_CONTENT)
async def post_logout(body: RefreshBody) -> None:
    async with session_scope() as session:
        await logout(session, refresh_raw=body.refresh_token)


@router.post("/auth/recover/start", response_model=RecoverStartOut)
async def post_recover_start(body: RecoverStartBody) -> RecoverStartOut:
    async with session_scope() as session:
        token = await start_recovery(session, auth_subject=body.auth_subject)
        return RecoverStartOut(recovery_token=token)


@router.post("/auth/recover/complete", status_code=status.HTTP_204_NO_CONTENT)
async def post_recover_complete(body: RecoverCompleteBody) -> None:
    async with session_scope() as session:
        await complete_recovery(
            session,
            raw_token=body.recovery_token,
            new_password=body.new_password,
        )


@router.post("/auth/change-password", status_code=status.HTTP_204_NO_CONTENT)
async def post_change_password(
    body: ChangePasswordBody,
    ctx: TenantContext = Depends(tenant_context_dep),
) -> None:
    if ctx.account_id is None:
        raise HTTPException(status_code=401, detail={"code": "AUTHENTICATION_REQUIRED"})
    async with session_scope() as session:
        await change_password(session, account_id=ctx.account_id, new_password=body.new_password)


# --- Tenant endpoints (PLT-A01) ---------------------------------------------


@router.post("/admin/tenants", response_model=TenantOut, status_code=status.HTTP_201_CREATED)
async def post_tenant(body: TenantCreateBody) -> TenantOut:
    """PLT-A01 — create tenant + initial ServicePrincipal owner. Platform-operator only.

    No auth on this endpoint in pilot — production uses mTLS + service token.
    """
    async with session_scope() as session:
        t = await create_tenant(session, display_name=body.display_name, default_timezone=body.default_timezone)
        return TenantOut(
            id=t.id,
            display_name=t.display_name,
            service_status=t.service_status.value,
            default_timezone=t.default_timezone,
        )


@router.post("/admin/tenants/{tenant_id}/activate", status_code=status.HTTP_204_NO_CONTENT)
async def post_activate_tenant(tenant_id: UUID) -> None:
    async with session_scope() as session:
        await activate_tenant(session, tenant_id=tenant_id)


@router.post(
    "/admin/tenants/{tenant_id}/units",
    response_model=EducationUnitOut,
    status_code=status.HTTP_201_CREATED,
)
async def post_education_unit(tenant_id: UUID, body: EducationUnitCreateBody) -> EducationUnitOut:
    async with session_scope() as session:
        u = await create_education_unit(
            session,
            tenant_id=tenant_id,
            unit_code=body.unit_code,
            name=body.name,
            institution_type=body.institution_type,
            education_levels=body.education_levels,
            timezone=body.timezone,
        )
        return EducationUnitOut(
            id=u.id,
            unit_code=u.unit_code,
            name=u.name,
            institution_type=u.institution_type,
            education_levels=u.education_levels,
            status=u.status.value,
            timezone=u.timezone,
        )


# --- Account / membership / grant endpoints (PLT-A05/06, tenancy context) -


@router.post(
    "/admin/accounts",
    response_model=AccountOut,
    status_code=status.HTTP_201_CREATED,
)
async def post_account(
    body: AccountCreateBody,
    ctx: TenantContext = Depends(tenant_context_dep),
) -> AccountOut:
    if ctx.actor_kind.value != "service_principal":
        raise HTTPException(
            status_code=403,
            detail={"code": "PLATFORM_OPERATOR_REQUIRED"},
        )
    async with session_scope() as session:
        a = await create_account(
            session,
            auth_subject=body.auth_subject,
            auth_namespace=body.auth_namespace,
            display_name=body.display_name,
            password=body.password,
        )
        if body.password is not None:
            await activate_account(session, account_id=a.id)
        return AccountOut(
            id=a.id,
            auth_subject=a.auth_subject,
            display_name=a.display_name,
            status=a.status.value,
            authz_revision=a.authz_revision,
        )


@router.post(
    "/me/tenants/{tenant_id}/memberships",
    response_model=MembershipOut,
    status_code=status.HTTP_201_CREATED,
)
async def post_invite(
    tenant_id: UUID,
    body: InviteBody,
    ctx: TenantContext = Depends(require_action(ActionCode.PLT_A05)),
) -> MembershipOut:
    async with session_scope() as session:
        m = await invite_membership(
            session,
            tenant_id=tenant_id,
            account_id=body.account_id,
            invitation_ref=body.invitation_ref,
        )
        return MembershipOut(
            id=m.id,
            tenant_id=m.tenant_id,
            account_id=m.account_id,
            status=m.status.value,
            valid_from=m.valid_from,
            valid_until=m.valid_until,
        )


@router.post(
    "/me/memberships/{membership_id}/activate",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def post_activate_membership(
    membership_id: UUID,
    ctx: TenantContext = Depends(require_action(ActionCode.PLT_A05)),
) -> None:
    async with session_scope() as session:
        await activate_membership(session, membership_id=membership_id)


@router.post(
    "/me/memberships/{membership_id}/revoke",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def post_revoke_membership(
    membership_id: UUID,
    reason: str,
    ctx: TenantContext = Depends(require_action(ActionCode.PLT_A06)),
) -> None:
    async with session_scope() as session:
        await revoke_membership(session, membership_id=membership_id, reason=reason)


@router.post(
    "/me/grants",
    response_model=GrantOut,
    status_code=status.HTTP_201_CREATED,
)
async def post_grant(
    body: GrantBody,
    ctx: TenantContext = Depends(require_action(ActionCode.PLT_A05)),
) -> GrantOut:
    async with session_scope() as session:
        g = await grant_access(
            session,
            tenant_id=ctx.tenant_id,
            membership_id=body.membership_id,
            role_code=body.role_code,
            action_ids=body.action_ids,
            scope=body.scope,
            policy_id=body.policy_id,
            authorization=body.authorization,
        )
        return GrantOut(
            id=g.id,
            membership_id=g.membership_id,
            service_principal_id=g.service_principal_id,
            role_code=g.role_code,
            action_ids=g.action_ids,
            scope=g.scope,
            status=g.status,
        )


@router.post(
    "/me/grants/{assignment_id}/revoke",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def post_revoke_grant(
    assignment_id: UUID,
    reason: str,
    ctx: TenantContext = Depends(require_action(ActionCode.PLT_A06)),
) -> None:
    async with session_scope() as session:
        await revoke_access(session, assignment_id=assignment_id, reason=reason)


@router.post(
    "/me/policies",
    response_model=PolicyOut,
    status_code=status.HTTP_201_CREATED,
)
async def post_policy(
    body: PolicyBody,
    ctx: TenantContext = Depends(require_action(ActionCode.PLT_A04)),
) -> PolicyOut:
    async with session_scope() as session:
        pv = await publish_policy(
            session,
            tenant_id=ctx.tenant_id,
            policy_domain=body.policy_domain,
            version_no=body.version_no,
            ruleset=body.ruleset,
            unit_scope_id=body.unit_scope_id,
            program_scope_id=body.program_scope_id,
            effective_from=body.effective_from,
            effective_until=body.effective_until,
        )
        return PolicyOut(
            id=pv.id,
            policy_domain=pv.policy_domain,
            version_no=pv.version_no,
            status=pv.status,
            ruleset=pv.ruleset,
        )


@router.get(
    "/me/audit",
    response_model=list[AuditOut],
)
async def get_audit(
    object_kind: str | None = None,
    object_id: UUID | None = None,
    limit: int = 100,
    ctx: TenantContext = Depends(require_action(ActionCode.PLT_A07)),
) -> list[AuditOut]:
    async with session_scope() as session:
        from sqlalchemy import select

        q = select(AuditRecordRow).where(AuditRecordRow.tenant_id == ctx.tenant_id)
        if object_kind is not None:
            q = q.where(AuditRecordRow.action_id.like(f"{object_kind}%"))
        if object_id is not None:
            q = q.where(AuditRecordRow.object_ref["id"].astext == str(object_id))
        q = q.order_by(AuditRecordRow.occurred_at.desc()).limit(limit)
        rows = (await session.execute(q)).scalars().all()
        return [
            AuditOut(
                id=r.id,
                actor=r.actor,
                action_id=r.action_id,
                object_ref=r.object_ref,
                occurred_at=r.occurred_at,
                reason=r.reason,
            )
            for r in rows
        ]