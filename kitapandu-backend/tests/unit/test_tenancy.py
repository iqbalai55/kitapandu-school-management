"""Unit tests for the TenantContext value object + actor kinds.

Seam: ``ksma.shared.tenancy``. DB-bound GUC helpers (``set_current_tenant``)
live in integration tests.
"""
from __future__ import annotations

from uuid import UUID, uuid4

import pytest

from ksma.shared.tenancy import ActorKind, TenantContext


def _ctx(**overrides) -> TenantContext:
    defaults = dict(
        account_id=uuid4(),
        membership_id=uuid4(),
        tenant_id=uuid4(),
        unit_id=None,
        actor_kind=ActorKind.ACCOUNT,
        authz_revision=1,
        role_codes=("AC",),
    )
    defaults.update(overrides)
    return TenantContext(**defaults)


class TestActorKind:
    def test_all_three_kinds_present(self) -> None:
        assert ActorKind.ACCOUNT.value == "account"
        assert ActorKind.SERVICE_PRINCIPAL.value == "service_principal"
        assert ActorKind.SUPPORT_OPERATOR.value == "support_operator"


class TestTenantContextPredicates:
    def test_is_service(self) -> None:
        ctx = _ctx(actor_kind=ActorKind.SERVICE_PRINCIPAL, account_id=None, membership_id=None)
        assert ctx.is_service() is True
        assert ctx.is_support() is False

    def test_is_support(self) -> None:
        ctx = _ctx(actor_kind=ActorKind.SUPPORT_OPERATOR, account_id=None, membership_id=None)
        assert ctx.is_support() is True
        assert ctx.is_service() is False

    def test_is_account(self) -> None:
        ctx = _ctx()
        assert ctx.is_service() is False
        assert ctx.is_support() is False

    def test_has_role_true(self) -> None:
        assert _ctx(role_codes=("AC", "FI")).has_role("AC") is True

    def test_has_role_false(self) -> None:
        assert _ctx(role_codes=("AC",)).has_role("FI") is False

    def test_has_role_empty(self) -> None:
        assert _ctx(role_codes=()).has_role("AC") is False


class TestTenantContextImmutability:
    def test_frozen(self) -> None:
        ctx = _ctx()
        with pytest.raises(Exception):  # FrozenInstanceError
            ctx.tenant_id = uuid4()  # type: ignore[misc]


class TestTenantContextOptionalFields:
    def test_unit_id_none(self) -> None:
        ctx = _ctx(unit_id=None)
        assert ctx.unit_id is None

    def test_unit_id_present(self) -> None:
        unit = uuid4()
        ctx = _ctx(unit_id=unit)
        assert ctx.unit_id == unit

    def test_role_codes_default_empty(self) -> None:
        ctx = TenantContext(
            account_id=uuid4(), membership_id=uuid4(),
            tenant_id=uuid4(), unit_id=None,
            actor_kind=ActorKind.ACCOUNT, authz_revision=1,
        )
        assert ctx.role_codes == ()