"""FastAPI dependencies for auth + RBAC.

Provides:
- ``bearer_token`` → raw token string from ``Authorization: Bearer …``
- ``current_account`` → ``Account`` row (403 if invalid/missing)
- ``tenant_context_dep`` → resolves ``TenantContext`` from JWT claims
- ``require_action(action_id, target_builder)`` → whole-grant RBAC check

Usage in routers::

    @router.get("/api/v1/...")
    async def handler(
        ctx: TenantContext = Depends(tenant_context_dep),
        _=Depends(require_action(ActionCode.REG_A01)),
    ):
        ...
"""
from __future__ import annotations

from collections.abc import Callable
from typing import Annotated, Any

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from ksma.identity.infrastructure.models import AccountRow, MembershipRow
from ksma.identity.infrastructure.repositories import (
    AccessAssignmentRepository,
    AccountRepository,
    MembershipRepository,
)
from ksma.shared.auth.jwt import decode_access_token
from ksma.shared.errors import (
    AccessDeniedError,
    AuthenticationError,
    TokenExpiredError,
    TokenRevokedError,
)
from ksma.shared.persistence import session_scope, set_current_tenant
from ksma.shared.rbac import (
    ActionCode,
    GrantRow,
    RoleCode,
    Scope,
    Target,
    evaluate,
)
from ksma.shared.tenancy import ActorKind, TenantContext

_bearer = HTTPBearer(auto_error=False)


async def bearer_token(
    creds: HTTPAuthorizationCredentials | None = Depends(_bearer),
) -> str:
    if creds is None or not creds.credentials:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"code": "AUTHENTICATION_REQUIRED"},
        )
    return creds.credentials


async def current_account_id(token: str = Depends(bearer_token)) -> Any:
    """Decode JWT → account UUID; raises 401 on failure."""
    try:
        claims = decode_access_token(token)
    except TokenExpiredError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"code": "TOKEN_EXPIRED"},
        )
    except TokenRevokedError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"code": "TOKEN_REVOKED"},
        )
    return claims


async def tenant_context_dep(
    request: Request,
    claims: dict[str, Any] = Depends(current_account_id),
) -> TenantContext:
    """Build a TenantContext from the JWT claims + verify against DB.

    Resolution steps:
    1. Extract ``tz_current_tenant``, ``tz_authz_revision``, ``tz_memberships`` from claims
    2. Open a session, load account row, check ``authz_revision`` matches
    3. Load the membership row for the current tenant
    4. Load active AccessAssignment rows for the membership (we do NOT preload grants
       here — the RBAC dep does that)
    5. Return TenantContext

    The session is closed at request end; tenant GUC is set per-transaction by the
    request handler via ``session_scope``.
    """
    sub = claims.get("sub")
    tenant_id = claims.get("tz_current_tenant")
    authz_rev = claims.get("tz_authz_revision")
    if not sub or not tenant_id or authz_rev is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"code": "INVALID_CLAIMS"},
        )
    async with session_scope() as session:
        # Load the account + verify authz_revision.
        from sqlalchemy import select

        account = (
            await session.execute(
                select(AccountRow).where(AccountRow.id == sub)
            )
        ).scalar_one_or_none()
        if account is None or account.status != "active":
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail={"code": "INVALID_CREDENTIALS"},
            )
        if int(account.authz_revision) != int(authz_rev):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail={"code": "AUTHZ_REVISION_MISMATCH"},
            )

        # Load the membership.
        membership = (
            await session.execute(
                select(MembershipRow).where(
                    MembershipRow.account_id == sub,
                    MembershipRow.tenant_id == tenant_id,
                )
            )
        ).scalar_one_or_none()
        if membership is None or membership.status != "active":
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail={"code": "MEMBERSHIP_INACTIVE"},
            )

        # Resolve current unit from URL or claim (URL wins for clarity).
        unit_id = claims.get("tz_current_unit")

        # Collect role_codes from claims.
        roles = tuple(
            (m.get("roles") or []) for m in claims.get("tz_memberships", [])
        )
        role_codes: tuple[str, ...] = tuple(
            r for sublist in roles for r in (sublist if isinstance(sublist, list) else [])
        )

        # Store the resolved tenant context in request.state for downstream handlers.
        ctx = TenantContext(
            account_id=sub,
            membership_id=membership.id,
            tenant_id=tenant_id,
            unit_id=unit_id,
            actor_kind=ActorKind.ACCOUNT,
            authz_revision=int(authz_rev),
            role_codes=role_codes,
        )
        request.state.tenant_context = ctx

        # Pre-set the GUC for any query in the same session — though downstream
        # code reopens a new session via session_scope(). The current session is closed.
        await set_current_tenant(session, tenant_id)
        return ctx


async def load_active_grants(
    ctx: TenantContext,
) -> tuple[GrantRow, ...]:
    """Load all active grants for the current membership, for the resolver."""
    async with session_scope() as session:
        repo = AccessAssignmentRepository(session, ctx.tenant_id)
        from sqlalchemy import select

        from ksma.identity.infrastructure.models import AccessAssignmentRow

        rows = (
            await session.execute(
                select(AccessAssignmentRow).where(
                    AccessAssignmentRow.membership_id == ctx.membership_id,
                    AccessAssignmentRow.status == "active",
                )
            )
        ).scalars().all()
        grants: list[GrantRow] = []
        for r in rows:
            try:
                grant = GrantRow(
                    id=r.id,
                    role_code=RoleCode(r.role_code),
                    action_ids=frozenset(ActionCode(a) for a in r.action_ids),
                    scope=Scope.from_json(r.scope or {}),
                    valid_from=r.valid_from,
                    valid_until=r.valid_until,
                    status=r.status,
                    policy_id=r.policy_id,
                )
            except (ValueError, KeyError):
                continue
            grants.append(grant)
        return tuple(grants)


def require_action(
    action: ActionCode,
    *,
    target_builder: Callable[[Request], Target] | None = None,
):
    """FastAPI dependency factory for RBAC.

    Returns a dependency that:
    1. Loads the tenant context from the JWT
    2. Loads active grants for the membership
    3. Builds the target from the request (via ``target_builder`` or default Target())
    4. Evaluates the whole grant per R02
    5. Raises 403 on no match — payload is generic (no leakage per OWASP R1)
    """

    async def _dep(
        request: Request,
        ctx: TenantContext = Depends(tenant_context_dep),
    ) -> TenantContext:
        target = target_builder(request) if target_builder else Target()
        grants = await load_active_grants(ctx)
        from ksma.shared.time import clock

        decision = evaluate(
            actor=ctx,
            action=action,
            target=target,
            now=clock().now(),
            grants=grants,
        )
        if not decision.allowed:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail={"code": "ACCESS_DENIED"},
            )
        return ctx

    return _dep