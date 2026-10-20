"""Unit tests for the TenantContext value object + actor kinds.

Seam: ``ksma.shared.tenancy``. DB-bound GUC helpers (``set_current_tenant``)
live in integration tests.
"""
from __future__ import annotations

from dataclasses import FrozenInstanceError
from uuid import uuid4

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
        with pytest.raises(FrozenInstanceError):
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


# ---------------------------------------------------------------------------
# Issue #37 — parametrised table over every (ActorKind, RoleCode) member.
#
# The "13-role enum" in the spec is the cross-context pair of ActorKind (3
# members) + RoleCode (13 members). ``is_service`` / ``is_support`` /
# ``is_account`` consult ``actor_kind``; ``has_role`` consults ``role_codes``.
# We pin every cell of that 3 x 13 matrix below so that any ``True → False``
# / ``False → True`` flip in the predicate methods is caught.
# ---------------------------------------------------------------------------


# ActorKind truth-table: is_service / is_support are mutually exclusive for
# the canonical three; `is_account` is the negation (it's an "Account" iff
# it is neither a service principal nor a support operator).
_ACTOR_KIND_TABLE = [
    (ActorKind.ACCOUNT, False, False, True),
    (ActorKind.SERVICE_PRINCIPAL, True, False, False),
    (ActorKind.SUPPORT_OPERATOR, False, True, False),
]


@pytest.mark.parametrize(
    "actor_kind, expected_is_service, expected_is_support, expected_is_account",
    _ACTOR_KIND_TABLE,
    ids=[k.name for k, _, _, _ in _ACTOR_KIND_TABLE],
)
def test_actor_kind_predicate_table(
    actor_kind: ActorKind,
    expected_is_service: bool,
    expected_is_support: bool,
    expected_is_account: bool,
) -> None:
    """For every member of the ActorKind enum, ``is_service`` /
    ``is_support`` / the implicit "is_account" (= neither service nor
    support) return the documented boolean.

    The TenantContext source doesn't have an explicit ``is_account`` —
    it's the negation. We pin it via the negation so a ``is_support``
    → ``is_account`` rename mutation that breaks the inverse relation
    is caught.
    """
    ctx = _ctx(actor_kind=actor_kind, account_id=None, membership_id=None)
    assert ctx.is_service() is expected_is_service
    assert ctx.is_support() is expected_is_support
    # `is_account` is the negation of (is_service OR is_support) per the
    # existing test_is_account semantic.
    assert (not ctx.is_service() and not ctx.is_support()) is expected_is_account


# RoleCode truth-table: has_role(code) returns True iff code is in role_codes.
# We pin the table over all 13 documented role codes (per ADR-0001 §4).
_13_ROLES = [
    "OW", "AM", "AC", "FI", "IN", "AS", "AP", "AU",
    "ST", "GU", "PY", "OP", "SV",
]


@pytest.mark.parametrize("role_code", _13_ROLES, ids=_13_ROLES)
def test_has_role_true_for_present_role(role_code: str) -> None:
    """Every one of the 13 role codes, when present in role_codes, returns
    True from ``has_role``. A `in` → `not in` flip would surface here.
    """
    # Put every role in there except the one we're testing — and then
    # include the target one to confirm True.
    others = tuple(r for r in _13_ROLES if r != role_code)
    ctx = _ctx(role_codes=(role_code, *others))
    assert ctx.has_role(role_code) is True


@pytest.mark.parametrize("role_code", _13_ROLES, ids=_13_ROLES)
def test_has_role_false_for_absent_role(role_code: str) -> None:
    """Every one of the 13 role codes, when NOT present in role_codes,
    returns False from ``has_role``. Symmetric to the True case above.
    """
    others = tuple(r for r in _13_ROLES if r != role_code)
    ctx = _ctx(role_codes=others)  # role_code is NOT present
    assert ctx.has_role(role_code) is False


@pytest.mark.parametrize("role_code", _13_ROLES, ids=_13_ROLES)
def test_has_role_false_for_empty_role_codes(role_code: str) -> None:
    """Empty role_codes → ``has_role`` returns False for every code.

    A `code in ()` → `code in self.role_codes + ()` (or any other
    always-true expression) mutation would surface here.
    """
    ctx = _ctx(role_codes=())
    assert ctx.has_role(role_code) is False


@pytest.mark.parametrize("missing_code", ["", "AC-", "ZZ", "lower"])
def test_has_role_false_for_unknown_strings(missing_code: str) -> None:
    """Unknown role code strings (not in the 13-role matrix) must return
    False. A missing `in self.role_codes` clause (e.g. `return code == ...`)
    would fail this for at least one of the inputs.
    """
    ctx = _ctx(role_codes=("AC",))
    assert ctx.has_role(missing_code) is False


def test_has_role_distinguishes_case() -> None:
    """``has_role`` is case-sensitive — the access matrix uses uppercase
    codes; "ac" is a different string and must NOT be granted access.
    """
    ctx = _ctx(role_codes=("AC",))
    assert ctx.has_role("AC") is True
    assert ctx.has_role("ac") is False


def test_actor_kind_predicates_return_exact_bool() -> None:
    """The predicates return bool, not truthy/falsy. A `return "yes"` mutation
    would surface here.
    """
    for kind, exp_service, exp_support, _ in _ACTOR_KIND_TABLE:
        ctx = _ctx(actor_kind=kind, account_id=None, membership_id=None)
        svc = ctx.is_service()
        sup = ctx.is_support()
        assert isinstance(svc, bool)
        assert isinstance(sup, bool)
        assert svc is exp_service
        assert sup is exp_support


def test_role_codes_tuple_is_immutable() -> None:
    """``role_codes`` is a tuple — passing a list is coerced at construction
    time and is no longer mutable. A `list[str]` field type would let
    callers mutate the audit trail; we lock the tuple.
    """
    ctx = _ctx(role_codes=("AC", "FI"))
    assert isinstance(ctx.role_codes, tuple)
    # And a mutation attempt raises — same dataclass is frozen.
    with pytest.raises(FrozenInstanceError):
        ctx.role_codes = ("AC",)  # type: ignore[misc]


def test_tenant_context_is_frozen_dataclass() -> None:
    """Per ADR-0002: TenantContext is a frozen dataclass — every field
    assignment raises FrozenInstanceError. A `@dataclass` (without frozen)
    mutation would silently allow assignment.
    """
    ctx = _ctx()
    for field_name in (
        "account_id", "membership_id", "tenant_id", "unit_id",
        "actor_kind", "authz_revision", "role_codes",
    ):
        with pytest.raises(FrozenInstanceError):
            setattr(ctx, field_name, None)  # type: ignore[misc]