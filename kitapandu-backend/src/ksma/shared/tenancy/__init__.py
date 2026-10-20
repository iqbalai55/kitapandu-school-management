"""Tenancy value object + per-request context.

``TenantContext`` carries the active tenant + unit + actor info resolved from the
JWT + session at the edge of every request. Application code never queries without
``ctx.tenant_id`` being set on the session via ``shared.persistence.set_current_tenant``.

The ``current_tenant`` dependency is registered with FastAPI in ``main.py`` and
provides ``TenantContext`` to downstream handlers.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from uuid import UUID


class ActorKind(StrEnum):
    ACCOUNT = "account"
    SERVICE_PRINCIPAL = "service_principal"
    SUPPORT_OPERATOR = "support_operator"  # logged separately for audit per ADR07


@dataclass(frozen=True, slots=True)
class TenantContext:
    """Resolved per-request: who am I, in what tenant, in what unit."""

    account_id: UUID | None  # None for service principals and support operators
    membership_id: UUID | None  # None for service principals
    tenant_id: UUID  # always present (even for service principals, after Admin setup)
    unit_id: UUID | None  # None when working tenant-wide (e.g., Finance dashboard)
    actor_kind: ActorKind
    authz_revision: int  # for token-cache invalidation (Q08)
    role_codes: tuple[str, ...] = ()

    def is_service(self) -> bool:
        return self.actor_kind is ActorKind.SERVICE_PRINCIPAL

    def is_support(self) -> bool:
        return self.actor_kind is ActorKind.SUPPORT_OPERATOR

    def has_role(self, code: str) -> bool:
        return code in self.role_codes

    @property
    def primary_role_code(self) -> str:
        """First granted role code, or ``"unknown"`` when the actor holds none.

        Single source of truth for the role code recorded on audit rows
        (``authorization_ref.role_code``); previously each audit call site
        inlined ``role_codes[0] if role_codes else "unknown"``.
        """
        return self.role_codes[0] if self.role_codes else "unknown"