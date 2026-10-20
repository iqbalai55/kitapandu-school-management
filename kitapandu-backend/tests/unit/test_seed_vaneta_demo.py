"""Unit tests for ``ksma.scripts.seed_vaneta_demo``.

Seam: ``ksma.scripts.seed_vaneta_demo`` — the *pure-Python* surface of
the dev-DB seed. The seed's mutation kill rate suffers from two
classes of resident mutants:

1. **Pure-Python helpers** (``_vaneta_participants``, dataclass
   constants, ``auth_subject_short``, ``_ctx_for`` actor-uuid shaping):
   these mutants are killed by directly testing the helper outputs.
2. **DB-bound helpers** (``_ensure_tenant``, ``_ensure_account``,
   ``_ensure_membership``, ``_ensure_invoice_with_payment_alloc``,
   etc.): these mutants are integration-side and require a live
   Postgres or an SQLAlchemy test double. They are NOT covered by
   these unit tests (the issue ticket explicitly defers
   DB-dependent mutants to a follow-up integration ticket).

Public surface under test (pure-Python only):
- module constants ``DEMO_PASSWORD``, ``BOOTSTRAP_SP_CODE``,
  ``BOOTSTRAP_SP_ENV``, ``BOOTSTRAP_SP_PURPOSE``
- ``TenantCfg`` (dataclass) and the ``TENANT_CFGS`` tuple
- ``DEMO_ROLE_CODES``
- ``POLICY_DOMAINS``
- ``_vaneta_participants()``
- ``auth_subject_short(account_id)``

Pure-Python; no DB / Docker / network — the DB-bound helpers are
NOT exercised here; only the constants and pure helpers are pinned.
"""
from __future__ import annotations

from dataclasses import FrozenInstanceError
from datetime import date
from uuid import UUID, uuid4

import pytest

from ksma.scripts.seed_vaneta_demo import (
    BOOTSTRAP_SP_CODE,
    BOOTSTRAP_SP_ENV,
    BOOTSTRAP_SP_PURPOSE,
    DEMO_PASSWORD,
    DEMO_ROLE_CODES,
    POLICY_DOMAINS,
    TENANT_CFGS,
    TenantCfg,
    _vaneta_participants,
    auth_subject_short,
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------


class TestBootstrapConstants:
    def test_demo_password(self) -> None:
        """The Argon2id-hashed password is shared across every demo
        account. Killing constant flips on this literal.
        """
        assert DEMO_PASSWORD == "Demo!2345"

    def test_bootstrap_service_principal_code(self) -> None:
        assert BOOTSTRAP_SP_CODE == "seed-bootstrap"

    def test_bootstrap_service_principal_env(self) -> None:
        assert BOOTSTRAP_SP_ENV == "development"

    def test_bootstrap_service_principal_purpose(self) -> None:
        assert BOOTSTRAP_SP_PURPOSE == "KSMA dev seed"


# ---------------------------------------------------------------------------
# TenantCfg dataclass + TENANT_CFGS
# ---------------------------------------------------------------------------


class TestTenantCfgDataclass:
    def test_tenant_cfg_field_set(self) -> None:
        """The dataclass carries exactly six documented fields. Mutating
        the type definition would expose extra / missing attributes.
        """
        cfg = TenantCfg(
            code="X",
            slug="x",
            display_name="X (DEMO)",
            unit_code="X-UNIT-A",
            institution_type="lk_lkp",
            seed_vaneta_data=False,
        )
        assert cfg.code == "X"
        assert cfg.slug == "x"
        assert cfg.display_name == "X (DEMO)"
        assert cfg.unit_code == "X-UNIT-A"
        assert cfg.institution_type == "lk_lkp"
        assert cfg.seed_vaneta_data is False

    def test_tenant_cfg_is_frozen(self) -> None:
        """``@dataclass(frozen=True, slots=True)`` — assigning after
        construction must raise.
        """
        cfg = TenantCfg(
            code="X",
            slug="x",
            display_name="X",
            unit_code="X-U",
            institution_type="lk_lkp",
            seed_vaneta_data=False,
        )
        with pytest.raises(FrozenInstanceError):
            cfg.code = "Y"  # type: ignore[misc]


class TestTenantConfigs:
    def test_exactly_two_tenant_configs(self) -> None:
        assert len(TENANT_CFGS) == 2

    def test_vaneta_tenant_is_first(self) -> None:
        """Ordering matters: the seed iterates ``TENANT_CFGS`` in tuple
        order, and Vaneta is the only tenant whose ``seed_vaneta_data``
        triggers the program + batch + finance branches.
        """
        vaneta = TENANT_CFGS[0]
        assert vaneta.code == "VAN"
        assert vaneta.slug == "vaneta"
        assert vaneta.display_name == "Vaneta Maju Berdaya (DEMO)"
        assert vaneta.unit_code == "VANETA-UNIT-A"
        assert vaneta.institution_type == "lk_lkp"
        # seed_vaneta_data is True for Vaneta only.
        assert vaneta.seed_vaneta_data is True

    def test_lembaga_tenant_is_second(self) -> None:
        lembaga = TENANT_CFGS[1]
        assert lembaga.code == "LMB"
        assert lembaga.slug == "lembaga-uji"
        assert lembaga.display_name == "Lembaga Uji Demo (PENGGANTI)"
        assert lembaga.unit_code == "LEMBAGA-UNIT-A"
        assert lembaga.institution_type == "lk_lkp"
        # Lembaga is the smoke-control tenant: no Vaneta-only data.
        assert lembaga.seed_vaneta_data is False

    def test_unit_codes_are_distinct(self) -> None:
        """``EducationUnit.unit_code`` is UNIQUE per ``tenant_id``;
        we have one unit per tenant and they must not collide.
        """
        unit_codes = {cfg.unit_code for cfg in TENANT_CFGS}
        assert len(unit_codes) == len(TENANT_CFGS)

    def test_slugs_are_distinct(self) -> None:
        """Slugs drive the demo-account ``auth_subject`` (e.g.
        ``ac.vaneta``); collision would break the smoke login matrix.
        """
        slugs = {cfg.slug for cfg in TENANT_CFGS}
        assert len(slugs) == len(TENANT_CFGS)

    def test_only_one_tenant_seeds_vaneta_data(self) -> None:
        """Exactly one tenant has ``seed_vaneta_data=True``. Killing
        a flip on this flag in either direction.
        """
        flag_counts = sum(
            1 for cfg in TENANT_CFGS if cfg.seed_vaneta_data
        )
        assert flag_counts == 1


# ---------------------------------------------------------------------------
# DEMO_ROLE_CODES
# ---------------------------------------------------------------------------


class TestDemoRoleCodes:
    def test_role_code_count_is_13(self) -> None:
        """Per the issue spec, 13 role codes (op/am/ow/ac/fi/in/as/ap/
        au/st/gu/py/sv). Killing mutation on the count.
        """
        assert len(DEMO_ROLE_CODES) == 13

    def test_role_codes_match_documented_order(self) -> None:
        """The seed iterates role codes in this exact order to seed 13
        demo accounts. Reordering or dropping a code would break the
        smoke login matrix.
        """
        assert DEMO_ROLE_CODES == (
            "op", "am", "ow", "ac", "fi", "in",
            "as", "ap", "au", "st", "gu", "py", "sv",
        )

    def test_role_codes_are_unique(self) -> None:
        assert len(set(DEMO_ROLE_CODES)) == len(DEMO_ROLE_CODES)


# ---------------------------------------------------------------------------
# POLICY_DOMAINS
# ---------------------------------------------------------------------------


class TestPolicyDomains:
    def test_policy_domain_count(self) -> None:
        """Six policy domains per the seed module. Killing mutation
        on the tuple length.
        """
        assert len(POLICY_DOMAINS) == 6

    def test_policy_domain_names(self) -> None:
        """Each ``POLICY_DOMAINS`` entry is a ``(name, ruleset)`` pair.
        We pin the names because the smoke pilot's spec depends on the
        access policy being published before any grant is issued.
        """
        domains = [name for name, _ruleset in POLICY_DOMAINS]
        assert domains == [
            "access",
            "finance",
            "enrollment",
            "session",
            "assessment",
            "certificate",
        ]

    def test_each_policy_has_a_dict_ruleset(self) -> None:
        for name, ruleset in POLICY_DOMAINS:
            assert isinstance(ruleset, dict), name
            assert ruleset, f"empty ruleset for {name}"

    @pytest.mark.parametrize(
        "domain,expected_key",
        [
            ("access", "version"),
            ("finance", "version"),
            ("finance", "currency"),
            ("finance", "default_installment_count"),
            ("enrollment", "version"),
            ("enrollment", "pause_supported"),
            ("session", "version"),
            ("session", "attendance_required_for_closure"),
            ("assessment", "version"),
            ("assessment", "rubric_required"),
            ("certificate", "version"),
            ("certificate", "numbering_scope"),
        ],
    )
    def test_policy_rulesets_carry_documented_keys(
        self, domain, expected_key
    ) -> None:
        """Each policy carries a fixed set of documented keys; a
        mutation that drops or renames a key would break the rule
        referenced by the corresponding service.
        """
        for name, ruleset in POLICY_DOMAINS:
            if name == domain:
                assert expected_key in ruleset, (
                    f"policy {name!r} missing documented key "
                    f"{expected_key!r}"
                )


# ---------------------------------------------------------------------------
# _vaneta_participants
# ---------------------------------------------------------------------------


class TestVanetaParticipants:
    """``_vaneta_participants()`` returns the 6 demo participant rows.

    The duplicate-suspect (V02) heuristic is wired up via the SHARED
    family email between P-0001 and P-0006 — keeping that link is
    essential to the smoke pilot's step_03 assertion.
    """

    def test_exactly_six_participants(self) -> None:
        rows = _vaneta_participants()
        assert len(rows) == 6

    def test_participant_nos_are_P0001_to_P0006(self) -> None:
        rows = _vaneta_participants()
        participant_nos = {row["participant_no"] for row in rows}
        assert participant_nos == {f"P-000{i}" for i in range(1, 7)}

    def test_participants_ordered_by_participant_no(self) -> None:
        rows = _vaneta_participants()
        assert [row["participant_no"] for row in rows] == [
            "P-0001", "P-0002", "P-0003", "P-0004", "P-0005", "P-0006",
        ]

    def test_p0001_and_p0006_share_family_email(self) -> None:
        """The shared email is the trigger for the V02 duplicate-suspect
        heuristic. Mutating the literal would break the search.
        """
        rows = _vaneta_participants()
        email_by_no = {
            row["participant_no"]: row["contact"]["email"]
            for row in rows
        }
        assert (
            email_by_no["P-0001"] == email_by_no["P-0006"]
        ), "shared email expected to drive the duplicate-suspect V02"
        assert "@" in email_by_no["P-0001"]
        assert "sari" in email_by_no["P-0001"].lower()

    def test_other_participants_have_distinct_emails(self) -> None:
        """P-0002..P-0005 should each have their own email (the shared
        link is restricted to the duplicate pair).
        """
        rows = _vaneta_participants()
        emails = [
            row["contact"]["email"]
            for row in rows
            if row["participant_no"] not in ("P-0001", "P-0006")
        ]
        assert len(set(emails)) == len(emails)

    def test_each_participant_has_required_fields(self) -> None:
        rows = _vaneta_participants()
        for row in rows:
            assert "participant_no" in row
            assert "full_name" in row
            assert "birth_date" in row
            assert isinstance(row["birth_date"], date)
            assert "contact" in row
            assert "email" in row["contact"]
            assert "phone" in row["contact"]

    @pytest.mark.parametrize("participant_no", ["P-0001", "P-0002", "P-0006"])
    def test_full_names_are_unique(self, participant_no: str) -> None:
        """Spot-check: at minimum the shared-email pair and P-0002
        must have distinct full names; mutating a literal would
        collapse these into each other.
        """
        rows = _vaneta_participants()
        full_names = {row["full_name"] for row in rows}
        assert len(full_names) == len(rows)
        # Sanity: confirm the parameter case is represented.
        assert any(
            row["participant_no"] == participant_no for row in rows
        )


# ---------------------------------------------------------------------------
# auth_subject_short
# ---------------------------------------------------------------------------


class TestAuthSubjectShort:
    """Pure-Python helper used in membership invitation_ref.

    ``str(account_id)[:8]`` truncates a UUID to the first 8 chars.
    Mutating the slice index (e.g. ``[:7]`` or ``[:9]``) would change
    every invitation_ref; this catches those.
    """

    def test_returns_first_eight_characters(self) -> None:
        aid = UUID("12345678-1234-5678-1234-567812345678")
        assert auth_subject_short(aid) == "12345678"

    def test_works_for_any_uuid(self) -> None:
        for hex_str in [
            "abcdef01-2345-6789-abcd-ef0123456789",
            "00000000-0000-0000-0000-000000000001",
            "ffffffff-ffff-ffff-ffff-ffffffffffff",
        ]:
            aid = UUID(hex_str)
            expected = hex_str.replace("-", "")[:8]
            assert auth_subject_short(aid) == expected

    def test_handles_uuid_from_uuid4(self) -> None:
        for _ in range(5):
            aid = uuid4()
            assert auth_subject_short(aid) == str(aid)[:8]
