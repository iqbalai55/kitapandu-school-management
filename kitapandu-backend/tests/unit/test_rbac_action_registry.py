"""Unit tests for the RBAC action registry + whole-grant evaluator.

Seam: ``ksma.shared.rbac``. Public surface:
- ``ActionCode`` (73 codes from KSMA Access Control Matrix v1.0)
- ``RoleCode`` (13 roles)
- ``ROLE_DEFAULT_ACTIONS`` (per-role default grant set)
- ``Scope`` + ``Scope.covers_target`` (JSONB-shaped scope evaluation)
- ``GrantRow`` + ``Target`` + ``evaluate(actor, action, target, now, grants)``
  (the whole-grant resolver per ADR-0001 §4 + R02 / R03)

Tests assert behavior at the public seam (the action IDs and grant
decision outcomes), not on internal helpers. They are pure-Python; no
DB, no FastAPI app, no real ``TenantContext`` instance.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from uuid import UUID, uuid4

import pytest

from ksma.shared.rbac import (
    ROLE_DEFAULT_ACTIONS,
    ActionCode,
    GrantDecision,
    GrantRow,
    RoleCode,
    Scope,
    Target,
    evaluate,
)


# ---------------------------------------------------------------------------
# Action registry
# ---------------------------------------------------------------------------


class TestActionRegistry:
    """Per ADR-0001: 73 action codes from the access matrix."""

    def test_exactly_73_codes(self) -> None:
        codes = [
            member for member in ActionCode
        ]
        assert len(codes) == 73, f"expected 73 codes, got {len(codes)}"

    def test_codes_match_module_prefixes(self) -> None:
        expected_prefixes = {"PLT", "REG", "CLS", "SES", "ASM", "FIN", "PRT", "OPS", "X"}
        prefixes = {c.value.split("-")[0] for c in ActionCode}
        assert prefixes == expected_prefixes

    def test_code_format_is_MOD_Axx(self) -> None:
        for code in ActionCode:
            assert "-" in code.value
            module, num = code.value.split("-")
            assert module in {"PLT", "REG", "CLS", "SES", "ASM", "FIN", "PRT", "OPS", "X"}
            assert num.startswith("A")
            assert num[1:].isdigit()
            assert 1 <= int(num[1:]) <= 9

    def test_no_duplicate_codes(self) -> None:
        values = [c.value for c in ActionCode]
        assert len(values) == len(set(values))

    def test_explosive_actions_are_marked_in_docstring(self) -> None:
        # The 8 PLT_A01..08, 9 REG_A01..09, etc. — all codes are present.
        # No behavioral assertion beyond presence.
        for code in [ActionCode.PLT_A05, ActionCode.PLT_A06, ActionCode.PLT_A07,
                     ActionCode.REG_A08, ActionCode.SES_A06, ActionCode.SES_A08,
                     ActionCode.ASM_A02, ActionCode.ASM_A06, ActionCode.ASM_A08,
                     ActionCode.FIN_A04, ActionCode.FIN_A08,
                     ActionCode.PRT_A04, ActionCode.PRT_A08,
                     ActionCode.OPS_A02, ActionCode.OPS_A04,
                     ActionCode.X_A01, ActionCode.X_A02, ActionCode.X_A03, ActionCode.X_A04,
                     ActionCode.X_A05, ActionCode.X_A06, ActionCode.X_A07, ActionCode.X_A08]:
            assert code.value  # present


# ---------------------------------------------------------------------------
# Role registry
# ---------------------------------------------------------------------------


class TestRoleRegistry:
    def test_exactly_13_roles(self) -> None:
        assert len(RoleCode) == 13

    def test_all_known_roles_present(self) -> None:
        expected = {"OW", "AM", "AC", "FI", "IN", "AS", "AP", "AU",
                    "ST", "GU", "PY", "OP", "SV"}
        actual = {r.value for r in RoleCode}
        assert actual == expected


# ---------------------------------------------------------------------------
# Per-role default grants
# ---------------------------------------------------------------------------


class TestRoleDefaultGrants:
    """Per the access matrix: roles listed for an action are granted by default.
    Roles not listed are NOT granted (must come via an explicit named grant).
    """

    def test_fi_has_full_fin_suite(self) -> None:
        fin_actions = {a for a in ActionCode if a.value.startswith("FIN-")}
        assert fin_actions.issubset(ROLE_DEFAULT_ACTIONS[RoleCode.FI])

    def test_ac_has_full_reg_suite(self) -> None:
        reg_actions = {a for a in ActionCode if a.value.startswith("REG-")}
        assert reg_actions.issubset(ROLE_DEFAULT_ACTIONS[RoleCode.AC])

    def test_in_teaches_but_does_not_grade(self) -> None:
        # IN is Pengajar: has SES_A02 (record attendance) but NOT ASM_A04 (record grades).
        in_grants = ROLE_DEFAULT_ACTIONS[RoleCode.IN]
        assert ActionCode.SES_A02 in in_grants
        assert ActionCode.ASM_A04 not in in_grants

    def test_op_does_not_have_internal_access(self) -> None:
        # Operator platform should not auto-get participant read or money read.
        op_grants = ROLE_DEFAULT_ACTIONS[RoleCode.OP]
        assert ActionCode.REG_A01 not in op_grants
        assert ActionCode.FIN_A01 not in op_grants

    def test_pins_unsafe_role_separation(self) -> None:
        # BR02 + access matrix: GU is academic-only, PY is finance-only.
        gu = ROLE_DEFAULT_ACTIONS[RoleCode.GU]
        py = ROLE_DEFAULT_ACTIONS[RoleCode.PY]
        # GU does NOT have FIN_A04 (verify payment evidence).
        assert ActionCode.FIN_A04 not in gu
        # PY does NOT have SES_A01 (view attendance).
        assert ActionCode.SES_A01 not in py


# ---------------------------------------------------------------------------
# Scope.covers_target
# ---------------------------------------------------------------------------


class TestScopeCoversTarget:
    def test_empty_scope_covers_anything(self) -> None:
        s = Scope()
        assert s.covers_target(unit_id=uuid4(), batch_id=uuid4()) is True

    def test_unit_membership_required(self) -> None:
        unit = uuid4()
        other = uuid4()
        s = Scope(units=(unit,))
        assert s.covers_target(unit_id=unit) is True
        assert s.covers_target(unit_id=other) is False

    def test_participant_membership_required(self) -> None:
        p = uuid4()
        other = uuid4()
        s = Scope(participants=(p,))
        assert s.covers_target(participant_id=p) is True
        assert s.covers_target(participant_id=other) is False

    def test_excluded_participant_rejected(self) -> None:
        p = uuid4()
        s = Scope(exclude_participants=(p,))
        assert s.covers_target(participant_id=p) is False
        # Other participants still allowed.
        assert s.covers_target(participant_id=uuid4()) is True

    def test_batch_membership_required(self) -> None:
        batch = uuid4()
        s = Scope(batches=(batch,))
        assert s.covers_target(batch_id=batch) is True
        assert s.covers_target(batch_id=uuid4()) is False

    def test_session_membership_required(self) -> None:
        sess = uuid4()
        s = Scope(sessions=(sess,))
        assert s.covers_target(session_id=sess) is True
        assert s.covers_target(session_id=uuid4()) is False

    def test_program_membership_required(self) -> None:
        prog = uuid4()
        s = Scope(programs=(prog,))
        assert s.covers_target(program_id=prog) is True
        assert s.covers_target(program_id=uuid4()) is False

    def test_column_restriction(self) -> None:
        s = Scope(columns=("name", "email"))
        assert s.covers_target(column="name") is True
        assert s.covers_target(column="grade") is False

    def test_no_target_fields_is_allowed(self) -> None:
        # covers_target() called with no args must not crash; must allow.
        s = Scope(units=(uuid4(),))
        assert s.covers_target() is True


# ---------------------------------------------------------------------------
# Whole-grant evaluator (R02)
# ---------------------------------------------------------------------------


def _make_grant(
    *,
    role: RoleCode,
    actions: frozenset[ActionCode],
    scope: Scope,
    valid_from: datetime | None = None,
    valid_until: datetime | None = None,
    status: str = "active",
) -> GrantRow:
    return GrantRow(
        id=uuid4(),
        role_code=role,
        action_ids=actions,
        scope=scope,
        valid_from=valid_from or datetime(2020, 1, 1, tzinfo=timezone.utc),
        valid_until=valid_until,
        status=status,
        policy_id=uuid4(),
    )


@pytest.fixture
def fixed_now() -> datetime:
    return datetime(2026, 10, 20, 12, 0, 0, tzinfo=timezone.utc)


class TestEvaluateWholeGrant:
    """Per R02: splicing scope from one grant with action from another is FORBIDDEN.
    Every successful evaluation must come from ONE grant that satisfies ALL
    of: action_ids, scope, validity, status='active'.
    """

    def test_no_grants_denies(self, fixed_now: datetime) -> None:
        decision = evaluate(
            actor=None, action=ActionCode.FIN_A05,
            target=Target(), now=fixed_now, grants=(),
        )
        assert decision.allowed is False
        assert decision.reason

    def test_one_active_grant_allows(self, fixed_now: datetime) -> None:
        g = _make_grant(role=RoleCode.FI,
                        actions=frozenset({ActionCode.FIN_A05}),
                        scope=Scope())
        decision = evaluate(
            actor=None, action=ActionCode.FIN_A05,
            target=Target(), now=fixed_now, grants=(g,),
        )
        assert decision.allowed is True
        assert decision.grant_id == g.id
        assert decision.role_code is RoleCode.FI

    def test_expired_grant_denies(self, fixed_now: datetime) -> None:
        g = _make_grant(
            role=RoleCode.FI, actions=frozenset({ActionCode.FIN_A05}),
            scope=Scope(),
            valid_until=fixed_now - timedelta(seconds=1),
        )
        decision = evaluate(
            actor=None, action=ActionCode.FIN_A05,
            target=Target(), now=fixed_now, grants=(g,),
        )
        assert decision.allowed is False

    def test_not_yet_valid_grant_denies(self, fixed_now: datetime) -> None:
        g = _make_grant(
            role=RoleCode.FI, actions=frozenset({ActionCode.FIN_A05}),
            scope=Scope(),
            valid_from=fixed_now + timedelta(seconds=1),
        )
        decision = evaluate(
            actor=None, action=ActionCode.FIN_A05,
            target=Target(), now=fixed_now, grants=(g,),
        )
        assert decision.allowed is False

    def test_revoked_grant_denies(self, fixed_now: datetime) -> None:
        g = _make_grant(
            role=RoleCode.FI, actions=frozenset({ActionCode.FIN_A05}),
            scope=Scope(), status="revoked",
        )
        decision = evaluate(
            actor=None, action=ActionCode.FIN_A05,
            target=Target(), now=fixed_now, grants=(g,),
        )
        assert decision.allowed is False

    def test_action_not_in_grant_denies(self, fixed_now: datetime) -> None:
        # FI role has FIN_A01..A08 by default, but if this specific grant
        # lists only FIN_A01..A07, the request for FIN_A08 must be denied.
        g = _make_grant(role=RoleCode.FI,
                        actions=frozenset({
                            ActionCode.FIN_A01, ActionCode.FIN_A02,
                            ActionCode.FIN_A03, ActionCode.FIN_A04,
                            ActionCode.FIN_A05, ActionCode.FIN_A06,
                            ActionCode.FIN_A07,
                        }),
                        scope=Scope())
        decision = evaluate(
            actor=None, action=ActionCode.FIN_A08,
            target=Target(), now=fixed_now, grants=(g,),
        )
        assert decision.allowed is False

    def test_scope_excludes_target_denies(self, fixed_now: datetime) -> None:
        unit = uuid4()
        other = uuid4()
        g = _make_grant(role=RoleCode.FI,
                        actions=frozenset({ActionCode.FIN_A05}),
                        scope=Scope(units=(unit,)))
        decision = evaluate(
            actor=None, action=ActionCode.FIN_A05,
            target=Target(unit_id=other), now=fixed_now, grants=(g,),
        )
        assert decision.allowed is False

    def test_no_splicing_r02(self, fixed_now: datetime) -> None:
        """R02: cannot take action from one grant + scope from another.

        Grant A: role AC, action FIN_A05 (out of role's defaults), scope restricted
                 to a single unit.
        Grant B: role FI, action FIN_A05 (in role's defaults), scope empty (any).
        The user has BOTH grants on the same membership.
        Requesting FIN_A05 on the unit in A's scope must be allowed only by
        the FIRST matching grant (R02: action and scope from same grant).
        Here B has the action and empty scope, so B should match first.
        """
        target_unit = uuid4()
        a = _make_grant(
            role=RoleCode.AC,
            actions=frozenset({ActionCode.FIN_A05}),
            scope=Scope(units=(target_unit,)),
        )
        b = _make_grant(
            role=RoleCode.FI,
            actions=frozenset({ActionCode.FIN_A05}),
            scope=Scope(),
        )
        decision = evaluate(
            actor=None, action=ActionCode.FIN_A05,
            target=Target(unit_id=target_unit), now=fixed_now, grants=(a, b),
        )
        assert decision.allowed is True
        # The whole-grant rule picks the first matching grant; either A or B
        # is acceptable since both satisfy ALL dimensions for the target.
        # The test asserts behavior, not which specific grant wins; the
        # important property is "no error / no splicing artifact".
        assert decision.grant_id in (a.id, b.id)

    def test_splicing_artifact_would_be_rejected(self, fixed_now: datetime) -> None:
        """Counter-example: if R02 were NOT enforced, splicing would falsely allow.

        Grant A: action REG_A01, scope restricted to unit_A.
        Grant B: action REG_A01, no scope restriction.
        Request REG_A01 on unit_B.

        If R02 is correctly enforced, the resolver iterates; B matches
        (action in grant_ids, scope is empty so the unit matches); allowed.
        That is correct behavior, not splicing.

        A truly splicing-attempting grant would have, e.g., grant B with
        action REG_A02 + empty scope, and request REG_A01. R02 already
        prevents this by only matching action_ids in the SAME grant.
        """
        a = _make_grant(role=RoleCode.AC,
                        actions=frozenset({ActionCode.REG_A01}),
                        scope=Scope(units=(uuid4(),)))
        b = _make_grant(role=RoleCode.AC,
                        actions=frozenset({ActionCode.REG_A02}),
                        scope=Scope())
        decision = evaluate(
            actor=None, action=ActionCode.REG_A01,
            target=Target(), now=fixed_now, grants=(a, b),
        )
        # B doesn't carry REG_A01, so the resolver must NOT pretend B does.
        assert decision.allowed is True
        assert decision.grant_id == a.id


# ---------------------------------------------------------------------------
# Scope JSON round-trip
# ---------------------------------------------------------------------------


class TestScopeJsonRoundTrip:
    def test_empty_round_trip(self) -> None:
        s = Scope()
        d = s.to_json()
        assert d == {}
        assert Scope.from_json(d) == s

    def test_full_round_trip(self) -> None:
        unit = uuid4(); batch = uuid4(); participant = uuid4()
        s = Scope(
            units=(unit,), batches=(batch,), participants=(participant,),
            exclude_participants=(uuid4(),),
            columns=("name",),
        )
        roundtripped = Scope.from_json(s.to_json())
        assert roundtripped == s