"""Unit tests for ``ksma.scripts.seed_vaneta_demo``.

Seam: ``ksma.scripts.seed_vaneta_demo``. Public surface:
- module constants ``DEMO_PASSWORD``, ``BOOTSTRAP_SP_CODE``,
  ``BOOTSTRAP_SP_ENV``, ``BOOTSTRAP_SP_PURPOSE``
- ``TenantCfg`` (dataclass) + the ``TENANT_CFGS`` tuple
- ``DEMO_ROLE_CODES``, ``POLICY_DOMAINS``
- ``_vaneta_participants()`` (6 demo participant payloads)
- ``auth_subject_short(account_id)`` (invitation_ref shaping)
- ``_ctx_for(...)`` (synthetic service-principal ``TenantContext``)
- the ``_ensure_*`` idempotent helpers and the ``_seed_vaneta``
  orchestrator, driven through a **duck-typed in-memory fake
  ``AsyncSession``** (``_FakeSession``) plus recording spies over the
  application-service functions they delegate to

Tests assert behavior at the public seam: the constants, the payload
shapes handed to the domain services, the branch decisions each
idempotent helper makes for a pre-existing row, and the returned
ids/statuses. They are pure-Python; no DB, no Docker, no network —
``_FakeSession`` records the emitted SQL + bound parameters instead of
executing them, so SQLAlchemy is only used for its ``text()``
constructor. Not asserted: real SQL execution, Alembic migrations,
and the RLS / ``set_current_tenant`` GUC round-trip (deferred to the
follow-up integration ticket).
"""
from __future__ import annotations

import json
from contextlib import asynccontextmanager
from dataclasses import FrozenInstanceError
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace
from uuid import UUID, uuid4, uuid5

import pytest

import ksma.scripts.seed_vaneta_demo as seed
from ksma.identity.domain import TenantServiceStatus
from ksma.scripts.seed_vaneta_demo import (
    BOOTSTRAP_SP_CODE,
    BOOTSTRAP_SP_ENV,
    BOOTSTRAP_SP_PURPOSE,
    DEMO_PASSWORD,
    DEMO_ROLE_CODES,
    POLICY_DOMAINS,
    TENANT_CFGS,
    TenantCfg,
    _bootstrap_operational_run,
    _ctx_for,
    _ensure_account,
    _ensure_application_for_participant,
    _ensure_invoice_with_payment_alloc,
    _ensure_membership,
    _ensure_participant,
    _ensure_published_program_version,
    _ensure_session,
    _ensure_tenant,
    _seed_vaneta,
    _vaneta_participants,
    auth_subject_short,
    main,
)
from ksma.session.domain import AttendanceCode
from ksma.shared.tenancy import ActorKind

# ---------------------------------------------------------------------------
# Test doubles
# ---------------------------------------------------------------------------


class _Scalar:
    """Marker for a ``SELECT ... scalar_one()`` answer."""

    def __init__(self, value):
        self.value = value


class _FakeResult:
    def __init__(self, row):
        self._row = row

    def first(self):
        return self._row

    def scalar_one(self):
        assert isinstance(self._row, _Scalar), (
            f"scalar_one() called on a non-scalar row: {self._row!r}"
        )
        return self._row.value


class _FakeSession:
    """Duck-typed ``AsyncSession``: records SQL, replays queued SELECT rows.

    ``select_rows`` is a FIFO queue consumed **only** by ``SELECT``
    statements; ``ALTER`` / ``DO`` / ``INSERT`` / ``UPDATE`` statements are
    recorded but never consume a slot. A queued item may be a row object
    (returned by ``.first()``), a ``_Scalar`` (returned by
    ``.scalar_one()``) or ``None`` (an empty ``SELECT``).
    """

    def __init__(self, select_rows=()):
        self.select_rows = list(select_rows)
        self.statements: list[tuple[str, dict | None]] = []
        self.flushes = 0

    async def execute(self, statement, params=None):
        sql = str(statement)
        self.statements.append((sql, params))
        row = None
        if sql.lstrip().upper().startswith("SELECT"):
            row = self.select_rows.pop(0) if self.select_rows else None
        return _FakeResult(row)

    async def flush(self):
        self.flushes += 1

    # -- assertion helpers ---------------------------------------------
    def sql_of(self, *statements: str) -> list[tuple[str, dict | None]]:
        return [s for s in self.statements if s in statements]

    def matching(self, needle: str) -> list[tuple[str, dict | None]]:
        return [(s, p) for s, p in self.statements if needle in s]


def _make_spy(result=None):
    """Recording async spy; ``result`` may be a value or a zero-arg factory."""
    calls: list[SimpleNamespace] = []

    async def _spy(*args, **kwargs):
        calls.append(SimpleNamespace(args=args, kwargs=kwargs))
        return result() if callable(result) else result

    _spy.calls = calls  # type: ignore[attr-defined]
    return _spy


def _make_spy_from(result):
    """Recording async spy whose result depends on the keyword arguments."""
    calls: list[SimpleNamespace] = []

    async def _spy(*args, **kwargs):
        calls.append(SimpleNamespace(args=args, kwargs=kwargs))
        return result(**kwargs)

    _spy.calls = calls  # type: ignore[attr-defined]
    return _spy


def _new_id() -> UUID:
    return uuid4()


_SEED_UUID_NS = UUID("11111111-2222-3333-4444-555555555555")


def _stable(kind: str, index) -> UUID:
    return uuid5(_SEED_UUID_NS, f"{kind}:{index}")


# Deterministic ids for the orchestrator doubles so "the first session" and
# "P-0001" are assertable identities, not just fresh UUIDs.
PARTICIPANT_IDS = {f"P-000{i}": _stable("participant", i) for i in range(1, 7)}
SESSION_IDS = {i: _stable("session", i) for i in (1, 2, 3)}
BATCH_ID = _stable("batch", 1)

AUDIT_ACTOR = UUID("00000000-0000-0000-0000-000000000001")
NULL_TENANT = UUID("00000000-0000-0000-0000-000000000000")


def _ctx() -> object:
    """A synthetic service-principal context, shaped by ``_ctx_for``."""
    return _ctx_for(
        tenant_id=uuid4(),
        unit_id=uuid4(),
        actor_id=AUDIT_ACTOR,
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


# ---------------------------------------------------------------------------
# _ctx_for — the synthetic service-principal TenantContext
# ---------------------------------------------------------------------------


class TestCtxFor:
    """``_ctx_for`` builds the ``TenantContext`` every application service
    in the seed receives. Application services stamp
    ``created_by = str(ctx.account_id)``, so the actor must be a concrete
    UUID (never ``None``) and the revision must be a usable value.
    """

    def test_actor_account_id_is_the_supplied_actor(self) -> None:
        ctx = _ctx_for(tenant_id=uuid4(), unit_id=None, actor_id=AUDIT_ACTOR)
        assert ctx.account_id == AUDIT_ACTOR
        assert str(ctx.account_id) == "00000000-0000-0000-0000-000000000001"

    def test_authz_revision_is_one(self) -> None:
        """The seed actor is a service principal with a fixed, non-zero
        authz revision; ``revision=0`` would look like an unresolved grant
        set to every service that re-reads it.
        """
        ctx = _ctx_for(tenant_id=uuid4(), unit_id=None, actor_id=uuid4())
        assert ctx.authz_revision == 1

    def test_actor_kind_is_service_principal(self) -> None:
        ctx = _ctx_for(tenant_id=uuid4(), unit_id=None, actor_id=uuid4())
        assert ctx.actor_kind == ActorKind.SERVICE_PRINCIPAL

    def test_membership_id_is_none(self) -> None:
        """No membership exists for the bootstrap actor, so the context
        must not claim one.
        """
        ctx = _ctx_for(tenant_id=uuid4(), unit_id=None, actor_id=uuid4())
        assert ctx.membership_id is None

    def test_role_codes_carry_the_seed_role(self) -> None:
        ctx = _ctx_for(tenant_id=uuid4(), unit_id=None, actor_id=uuid4())
        assert ctx.role_codes == ("SV",)

    def test_tenant_and_unit_ids_pass_through(self) -> None:
        tenant_id, unit_id = uuid4(), uuid4()
        ctx = _ctx_for(tenant_id=tenant_id, unit_id=unit_id, actor_id=uuid4())
        assert ctx.tenant_id == tenant_id
        assert ctx.unit_id == unit_id

    def test_unit_id_may_be_none(self) -> None:
        ctx = _ctx_for(tenant_id=uuid4(), unit_id=None, actor_id=uuid4())
        assert ctx.unit_id is None


# ---------------------------------------------------------------------------
# _ensure_participant — synthetic audit actor
# ---------------------------------------------------------------------------


class TestEnsureParticipant:
    """``_ensure_participant`` delegates to ``create_participant`` with a
    hand-built ``CreateParticipantCommand``. The command's actor carries
    ``authz_revision=1``; the whole point of the synthetic actor is that
    ``created_by`` is a real UUID string rather than ``"None"``.
    """

    def _run(self, monkeypatch, select_rows):
        spy = _make_spy(lambda: SimpleNamespace(id=uuid4()))
        monkeypatch.setattr(seed, "create_participant", spy)
        session = _FakeSession(select_rows)
        return spy, session

    @pytest.mark.asyncio
    async def test_new_participant_actor_carries_revision_one(
        self, monkeypatch
    ) -> None:
        spy, session = self._run(monkeypatch, [None])
        created = await _ensure_participant(
            session,
            tenant_id=uuid4(),
            participant_no="P-0001",
            full_name="Sari Wulandari",
            birth_date=date(2015, 3, 1),
            contact={"email": "sari@example.test"},
        )
        assert len(spy.calls) == 1
        cmd = spy.calls[0].args[1]
        assert cmd.actor.authz_revision == 1
        assert cmd.actor.actor_kind == ActorKind.SERVICE_PRINCIPAL
        assert cmd.actor.account_id == AUDIT_ACTOR
        assert str(cmd.actor.account_id) != "None"
        assert cmd.participant_no == "P-0001"

    @pytest.mark.asyncio
    async def test_new_participant_payload_is_forwarded(self, monkeypatch) -> None:
        spy, session = self._run(monkeypatch, [None])
        born = date(2015, 3, 1)
        await _ensure_participant(
            session,
            tenant_id=uuid4(),
            participant_no="P-0002",
            full_name="Budi Santoso",
            birth_date=born,
            contact={"email": "budi@example.test", "phone": "+62"},
        )
        cmd = spy.calls[0].args[1]
        assert cmd.full_name == "Budi Santoso"
        assert cmd.birth_date == born
        assert cmd.contact == {"email": "budi@example.test", "phone": "+62"}

    @pytest.mark.asyncio
    async def test_new_participant_returns_the_created_id(
        self, monkeypatch
    ) -> None:
        created_id = uuid4()
        monkeypatch.setattr(
            seed,
            "create_participant",
            _make_spy(lambda: SimpleNamespace(id=created_id)),
        )
        session = _FakeSession([None])
        returned = await _ensure_participant(
            session,
            tenant_id=uuid4(),
            participant_no="P-0001",
            full_name="Sari Wulandari",
            birth_date=date(2015, 3, 1),
            contact={"email": "sari@example.test"},
        )
        assert returned == created_id
        assert session.flushes == 1

    @pytest.mark.asyncio
    async def test_existing_participant_is_reused_not_recreated(
        self, monkeypatch
    ) -> None:
        existing = uuid4()
        spy, session = self._run(
            monkeypatch, [SimpleNamespace(id=str(existing))]
        )
        returned = await _ensure_participant(
            session,
            tenant_id=uuid4(),
            participant_no="P-0003",
            full_name="Rina",
            birth_date=None,
            contact=None,
        )
        assert returned == existing
        assert spy.calls == []


# ---------------------------------------------------------------------------
# _ensure_tenant / _ensure_account / _ensure_membership — reactivation
# ---------------------------------------------------------------------------


class TestEnsureTenantReactivation:
    """The lookup path re-activates a pre-existing tenant only when its
    ``service_status`` is **not** already ``active``. A tenant left in
    ``setup``/``suspended`` must be flipped to ``active`` so the pilot
    login works.
    """

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "stale_status",
        [TenantServiceStatus.SETUP.value, TenantServiceStatus.SUSPENDED.value],
    )
    async def test_non_active_tenant_is_activated(self, stale_status) -> None:
        existing = uuid4()
        session = _FakeSession(
            [SimpleNamespace(id=str(existing), service_status=stale_status)]
        )
        returned = await _ensure_tenant(
            session,
            display_name="Vaneta Maju Berdaya (DEMO)",
            default_timezone="Asia/Jakarta",
        )
        assert returned == existing
        activations = session.matching("SET service_status = 'active'")
        assert len(activations) == 1, (
            f"tenant in {stale_status!r} must be activated"
        )
        assert activations[0][1]["id"] == str(existing)

    @pytest.mark.asyncio
    async def test_already_active_tenant_is_left_untouched(self) -> None:
        existing = uuid4()
        session = _FakeSession(
            [
                SimpleNamespace(
                    id=str(existing),
                    service_status=TenantServiceStatus.ACTIVE.value,
                )
            ]
        )
        returned = await _ensure_tenant(
            session,
            display_name="Vaneta Maju Berdaya (DEMO)",
            default_timezone="Asia/Jakarta",
        )
        assert returned == existing
        assert session.matching("UPDATE tenant") == []


class TestEnsureAccountActivation:
    """An account found in ``pending`` must be promoted to ``active``;
    an account already ``active`` must not be rewritten.
    """

    @pytest.mark.asyncio
    async def test_pending_account_is_activated(self) -> None:
        existing = uuid4()
        session = _FakeSession(
            [SimpleNamespace(id=str(existing), status="pending")]
        )
        returned = await _ensure_account(
            session,
            auth_subject="ac.vaneta",
            display_name="Budi Santoso",
            password=DEMO_PASSWORD,
        )
        assert returned == existing
        activations = session.matching("UPDATE account SET status = 'active'")
        assert len(activations) == 1
        assert activations[0][1]["id"] == str(existing)

    @pytest.mark.asyncio
    async def test_active_account_is_left_untouched(self) -> None:
        existing = uuid4()
        session = _FakeSession(
            [SimpleNamespace(id=str(existing), status="active")]
        )
        returned = await _ensure_account(
            session,
            auth_subject="ac.vaneta",
            display_name="Budi Santoso",
            password=DEMO_PASSWORD,
        )
        assert returned == existing
        assert session.matching("UPDATE account") == []


class TestEnsureMembershipActivation:
    """A membership left ``invited`` must be activated **and** the owning
    account's ``authz_revision`` bumped so JWT holders re-resolve.
    """

    @pytest.mark.asyncio
    async def test_invited_membership_is_activated_and_bumps_revision(
        self,
    ) -> None:
        membership_id, account_id = uuid4(), uuid4()
        session = _FakeSession(
            [SimpleNamespace(id=str(membership_id), status="invited")]
        )
        returned = await _ensure_membership(
            session, tenant_id=uuid4(), account_id=account_id
        )
        assert returned == membership_id
        activates = session.matching(
            "UPDATE membership SET status = 'active'"
        )
        bumps = session.matching("authz_revision = authz_revision + 1")
        assert len(activates) == 1
        assert len(bumps) == 1, "invited membership must bump authz_revision"
        assert bumps[0][1]["id"] == str(account_id)

    @pytest.mark.asyncio
    async def test_active_membership_is_left_untouched(self) -> None:
        membership_id = uuid4()
        session = _FakeSession(
            [SimpleNamespace(id=str(membership_id), status="active")]
        )
        returned = await _ensure_membership(
            session, tenant_id=uuid4(), account_id=uuid4()
        )
        assert returned == membership_id
        assert session.matching("UPDATE membership") == []
        assert session.matching("authz_revision = authz_revision + 1") == []


# ---------------------------------------------------------------------------
# _bootstrap_operational_run — attempt_count bump
# ---------------------------------------------------------------------------


class TestBootstrapOperationalRunAttemptCount:
    """Re-running the seed bumps ``attempt_count`` by exactly one and
    records the new attempt number in the ``evidence`` JSONB.
    """

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "current_attempt,expected_new",
        [
            (None, 1),  # NULL attempt_count is treated as zero
            (0, 1),  # falsy zero still yields attempt 1
            (1, 2),
            (3, 4),
            (41, 42),
        ],
    )
    async def test_attempt_count_is_incremented(
        self, current_attempt, expected_new
    ) -> None:
        run_id = uuid4()
        session = _FakeSession(
            [
                SimpleNamespace(
                    id=str(run_id), attempt_count=current_attempt
                )
            ]
        )
        returned = await _bootstrap_operational_run(
            session, sp_id=uuid4()
        )
        assert returned == run_id
        updates = session.matching("UPDATE operational_run")
        assert len(updates) == 1
        params = updates[0][1]
        assert params["n"] == expected_new
        evidence = json.loads(params["ev"])
        assert evidence == {"seed_event": f"attempt {expected_new}"}

    @pytest.mark.asyncio
    async def test_seed_constraint_is_widened_before_use(self) -> None:
        """``operation_kind='seed'`` is outside the original CHECK, so the
        seed must widen it before touching ``operational_run``.
        """
        session = _FakeSession([SimpleNamespace(id=str(uuid4()), attempt_count=1)])
        await _bootstrap_operational_run(session, sp_id=uuid4())
        sql_texts = [s for s, _ in session.statements]
        alter_at = next(
            i for i, s in enumerate(sql_texts) if "ADD COLUMN IF NOT EXISTS" in s
        )
        constraint_at = next(
            i
            for i, s in enumerate(sql_texts)
            if "ck_op_run_kind_seed" in s
        )
        select_at = next(
            i for i, s in enumerate(sql_texts) if s.strip().upper().startswith("SELECT")
        )
        assert alter_at < select_at
        assert constraint_at < select_at


# ---------------------------------------------------------------------------
# _seed_vaneta — the Vaneta-only orchestrator payload
# ---------------------------------------------------------------------------


def _spies(monkeypatch) -> dict[str, object]:
    """Replace every application service the orchestrator delegates to
    with a recording spy so the *arguments* the seed builds can be
    asserted without a database.
    """
    spies = {
        "participant": _make_spy_from(
            lambda participant_no, **_: PARTICIPANT_IDS[participant_no]
        ),
        "program": _make_spy(_new_id),
        "program_version": _make_spy(_new_id),
        "batch": _make_spy(BATCH_ID),
        "batch_status": _make_spy("planned"),
        "open_batch": _make_spy(_new_id),
        "session": _make_spy_from(lambda ordinal, **_: SESSION_IDS[ordinal]),
        "enrollment": _make_spy(_new_id),
        "invoice": _make_spy(_new_id),
        "attendance": _make_spy(_new_id),
        "allocation": _make_spy({"id": "alloc-0001"}),
        "inbox": _make_spy(None),
    }
    monkeypatch.setattr(seed, "_ensure_participant", spies["participant"])
    monkeypatch.setattr(seed, "_ensure_program", spies["program"])
    monkeypatch.setattr(seed, "_ensure_published_program_version", spies["program_version"])
    monkeypatch.setattr(seed, "_ensure_batch", spies["batch"])
    monkeypatch.setattr(seed, "_batch_status", spies["batch_status"])
    monkeypatch.setattr(seed, "open_batch", spies["open_batch"])
    monkeypatch.setattr(seed, "_ensure_session", spies["session"])
    monkeypatch.setattr(seed, "_ensure_enrollment_for_batch", spies["enrollment"])
    monkeypatch.setattr(seed, "_ensure_invoice_with_payment_alloc", spies["invoice"])
    monkeypatch.setattr(seed, "_ensure_attendance", spies["attendance"])
    monkeypatch.setattr(seed, "_allocation_row_for_first_invoice", spies["allocation"])
    monkeypatch.setattr(seed, "_seed_portal_inbox_and_audit", spies["inbox"])
    return spies


async def _run_seed_vaneta(monkeypatch):
    cfg = TENANT_CFGS[0]
    spies = _spies(monkeypatch)
    await _seed_vaneta(
        _FakeSession(),
        cfg=cfg,
        tenant_id=uuid4(),
        unit_id=uuid4(),
        ctx=_ctx(),
        demo={"st": {"account_id": uuid4()}},
        policy_ids={},
    )
    return spies


class TestSeedVanetaBatchOpening:
    """A freshly created batch is ``planned``; the orchestrator opens it
    so enrollment can be accepted.
    """

    @pytest.mark.asyncio
    async def test_planned_batch_is_opened(self, monkeypatch) -> None:
        spies = await _run_seed_vaneta(monkeypatch)
        assert len(spies["open_batch"].calls) == 1
        assert spies["open_batch"].calls[0].args[2] == BATCH_ID
        assert spies["batch"].calls[0].kwargs["batch_code"] == "EN-A1-Okt2026"

    @pytest.mark.asyncio
    async def test_already_open_batch_is_not_reopened(self, monkeypatch) -> None:
        spies = _spies(monkeypatch)
        monkeypatch.setattr(seed, "_batch_status", _make_spy("open"))
        await _seed_vaneta(
            _FakeSession(),
            cfg=TENANT_CFGS[0],
            tenant_id=uuid4(),
            unit_id=uuid4(),
            ctx=_ctx(),
            demo={"st": {"account_id": uuid4()}},
            policy_ids={},
        )
        assert spies["open_batch"].calls == []


class TestSeedVanetaSessions:
    """Three sessions are scheduled at now+7d/14d/21d, each running
    09:00-11:00 local-ish (ordinals 1..3).
    """

    @pytest.mark.asyncio
    async def test_three_sessions_are_seeded(self, monkeypatch) -> None:
        spies = await _run_seed_vaneta(monkeypatch)
        assert len(spies["session"].calls) == 3

    @pytest.mark.asyncio
    async def test_session_ordinals_are_one_based(
        self, monkeypatch
    ) -> None:
        spies = await _run_seed_vaneta(monkeypatch)
        assert [
            c.kwargs["ordinal"] for c in spies["session"].calls
        ] == [1, 2, 3]

    @pytest.mark.asyncio
    async def test_sessions_are_one_week_apart(self, monkeypatch) -> None:
        spies = await _run_seed_vaneta(monkeypatch)
        starts = [c.kwargs["starts_at"] for c in spies["session"].calls]
        gaps = [
            (b - a).days for a, b in zip(starts, starts[1:])
        ]
        assert gaps == [7, 7]

    @pytest.mark.asyncio
    async def test_first_session_is_seven_days_out(
        self, monkeypatch
    ) -> None:
        spies = await _run_seed_vaneta(monkeypatch)
        started = datetime.now(timezone.utc)
        first = spies["session"].calls[0].kwargs["starts_at"]
        offset = first - started
        assert timedelta(days=7, hours=1) - timedelta(seconds=5) <= offset
        assert offset <= timedelta(days=7, hours=1) + timedelta(seconds=5)

    @pytest.mark.asyncio
    async def test_each_session_runs_for_two_hours(self, monkeypatch) -> None:
        """``starts_at`` is +1h and ``ends_at`` is +3h from the anchor, so
        every session is exactly a 2-hour block.
        """
        spies = await _run_seed_vaneta(monkeypatch)
        for call in spies["session"].calls:
            duration = call.kwargs["ends_at"] - call.kwargs["starts_at"]
            assert duration == timedelta(hours=2)

    @pytest.mark.asyncio
    async def test_session_timestamps_have_no_microseconds(
        self, monkeypatch
    ) -> None:
        """The anchor ``now`` is truncated to whole seconds so re-running
        the seed produces byte-identical timestamps.
        """
        spies = await _run_seed_vaneta(monkeypatch)
        for call in spies["session"].calls:
            assert call.kwargs["starts_at"].microsecond == 0
            assert call.kwargs["ends_at"].microsecond == 0

    @pytest.mark.asyncio
    async def test_all_sessions_share_the_seeded_batch(
        self, monkeypatch
    ) -> None:
        spies = await _run_seed_vaneta(monkeypatch)
        assert {
            c.kwargs["batch_id"] for c in spies["session"].calls
        } == {BATCH_ID}


class TestSeedVanetaInvoices:
    """Two invoices: P-0001 fully paid (1 x 1,000,000), P-0002 unpaid
    across two installments totalling 1,000,000.
    """

    @pytest.mark.asyncio
    async def test_two_invoices_are_seeded(self, monkeypatch) -> None:
        spies = await _run_seed_vaneta(monkeypatch)
        assert len(spies["invoice"].calls) == 2

    @pytest.mark.asyncio
    async def test_first_invoice_is_single_line_full_amount(
        self, monkeypatch
    ) -> None:
        spies = await _run_seed_vaneta(monkeypatch)
        first = spies["invoice"].calls[0].kwargs
        assert first["target_invoice_no"] == "INV-VANETA-0001"
        assert first["lines"] == [
            {
                "description": "EN-A1 Tuition — Sari Wulandari",
                "quantity": 1,
                "unit_price": 1_000_000,
                "discount_amount": 0,
            }
        ]

    @pytest.mark.asyncio
    async def test_first_invoice_single_installment_is_fully_allocated(
        self, monkeypatch
    ) -> None:
        spies = await _run_seed_vaneta(monkeypatch)
        first = spies["invoice"].calls[0].kwargs
        assert len(first["installments"]) == 1
        seq_no, due_date, due_amount = first["installments"][0]
        assert seq_no == 1
        assert due_amount == 1_000_000
        assert isinstance(due_date, date)
        assert first["allocation_amount"] == 1_000_000

    @pytest.mark.asyncio
    async def test_second_invoice_has_two_termin_installments(
        self, monkeypatch
    ) -> None:
        spies = await _run_seed_vaneta(monkeypatch)
        second = spies["invoice"].calls[1].kwargs
        assert second["target_invoice_no"] == "INV-VANETA-0002"
        assert [line["unit_price"] for line in second["lines"]] == [600_000, 400_000]
        assert [line["quantity"] for line in second["lines"]] == [1, 1]
        assert [line["discount_amount"] for line in second["lines"]] == [0, 0]
        seqs = [i[0] for i in second["installments"]]
        amounts = [i[2] for i in second["installments"]]
        assert seqs == [1, 2]
        assert amounts == [600_000, 400_000]

    @pytest.mark.asyncio
    async def test_second_invoice_is_left_unallocated(self, monkeypatch) -> None:
        """Per the pilot ticket, INV-VANETA-0002 stays unpaid so the
        dashboard shows a genuinely outstanding invoice.
        """
        spies = await _run_seed_vaneta(monkeypatch)
        assert spies["invoice"].calls[1].kwargs["allocation_amount"] is None

    @pytest.mark.asyncio
    async def test_installment_due_dates_are_the_seed_day(
        self, monkeypatch
    ) -> None:
        spies = await _run_seed_vaneta(monkeypatch)
        today = datetime.now(timezone.utc).date()
        for call in spies["invoice"].calls:
            for _seq, due_date, _amt in call.kwargs["installments"]:
                assert abs((due_date - today).days) <= 1


class TestSeedVanetaAttendance:
    """Exactly one attendance row, for P-0001, in the **first** session.
    """

    @pytest.mark.asyncio
    async def test_attendance_recorded_once(self, monkeypatch) -> None:
        spies = await _run_seed_vaneta(monkeypatch)
        assert len(spies["attendance"].calls) == 1

    @pytest.mark.asyncio
    async def test_attendance_targets_the_first_session(
        self, monkeypatch
    ) -> None:
        spies = await _run_seed_vaneta(monkeypatch)
        assert (
            spies["attendance"].calls[0].kwargs["session_id"]
            == SESSION_IDS[1]
        )

    @pytest.mark.asyncio
    async def test_attendance_records_presence_for_p0001(
        self, monkeypatch
    ) -> None:
        spies = await _run_seed_vaneta(monkeypatch)
        call = spies["attendance"].calls[0]
        assert (
            call.kwargs["participant_id"] == PARTICIPANT_IDS["P-0001"]
        )
        assert call.kwargs["batch_id"] == BATCH_ID
        assert call.kwargs["code"] == AttendanceCode.HADIR


class TestSeedVanetaParticipantAndEnrollmentShape:
    @pytest.mark.asyncio
    async def test_all_six_participants_are_seeded(self, monkeypatch) -> None:
        spies = await _run_seed_vaneta(monkeypatch)
        assert [c.kwargs["participant_no"] for c in spies["participant"].calls] == [
            f"P-000{i}" for i in range(1, 7)
        ]

    @pytest.mark.asyncio
    async def test_only_p0001_and_p0002_are_enrolled(self, monkeypatch) -> None:
        spies = await _run_seed_vaneta(monkeypatch)
        enrolled = [
            c.kwargs["participant_id"] for c in spies["enrollment"].calls
        ]
        assert enrolled == [
            PARTICIPANT_IDS["P-0001"],
            PARTICIPANT_IDS["P-0002"],
        ]


# ---------------------------------------------------------------------------
# _ensure_published_program_version — publish flags
# ---------------------------------------------------------------------------


class TestEnsurePublishedProgramVersion:
    @pytest.mark.asyncio
    async def test_new_version_is_published_as_current(
        self, monkeypatch
    ) -> None:
        pv_id = uuid4()
        publish = _make_spy(_new_id)
        monkeypatch.setattr(
            seed,
            "create_program_version",
            _make_spy(lambda: SimpleNamespace(id=pv_id)),
        )
        monkeypatch.setattr(seed, "publish_program_version", publish)
        session = _FakeSession([None])
        returned = await _ensure_published_program_version(
            session, ctx=_ctx(), program_id=uuid4()
        )
        assert returned == pv_id
        assert len(publish.calls) == 1
        assert publish.calls[0].kwargs["make_current"] is True
        assert publish.calls[0].kwargs["version_id"] == pv_id

    @pytest.mark.asyncio
    async def test_existing_version_is_not_republished(
        self, monkeypatch
    ) -> None:
        pv_id = uuid4()
        publish = _make_spy(_new_id)
        monkeypatch.setattr(seed, "create_program_version", _make_spy())
        monkeypatch.setattr(seed, "publish_program_version", publish)
        session = _FakeSession(
            [SimpleNamespace(id=str(pv_id), status="published")]
        )
        returned = await _ensure_published_program_version(
            session, ctx=_ctx(), program_id=uuid4()
        )
        assert returned == pv_id
        assert publish.calls == []


# ---------------------------------------------------------------------------
# _ensure_session — revision flags
# ---------------------------------------------------------------------------


class TestEnsureSessionRevision:
    @pytest.mark.asyncio
    async def test_new_session_gets_an_effective_revision(
        self, monkeypatch
    ) -> None:
        sid = uuid4()
        add_revision = _make_spy(_new_id)
        monkeypatch.setattr(
            seed, "create_session", _make_spy(lambda: SimpleNamespace(id=sid))
        )
        monkeypatch.setattr(seed, "add_revision", add_revision)
        session = _FakeSession([None])
        starts = datetime(2026, 10, 27, 1, 0, tzinfo=timezone.utc)
        returned = await _ensure_session(
            session,
            ctx=_ctx(),
            batch_id=uuid4(),
            ordinal=1,
            starts_at=starts,
            ends_at=starts + timedelta(hours=2),
        )
        assert returned == sid
        assert len(add_revision.calls) == 1
        kwargs = add_revision.calls[0].kwargs
        assert kwargs["make_effective"] is True
        assert kwargs["starts_at"] == starts
        assert kwargs["ends_at"] == starts + timedelta(hours=2)
        assert kwargs["timezone_str"] == "Asia/Jakarta"

    @pytest.mark.asyncio
    async def test_existing_session_without_revision_gets_one(
        self, monkeypatch
    ) -> None:
        sid = uuid4()
        add_revision = _make_spy(_new_id)
        monkeypatch.setattr(seed, "create_session", _make_spy())
        monkeypatch.setattr(seed, "add_revision", add_revision)
        session = _FakeSession(
            [SimpleNamespace(id=str(sid)), None]
        )
        starts = datetime(2026, 10, 27, 1, 0, tzinfo=timezone.utc)
        returned = await _ensure_session(
            session,
            ctx=_ctx(),
            batch_id=uuid4(),
            ordinal=1,
            starts_at=starts,
            ends_at=starts + timedelta(hours=2),
        )
        assert returned == sid
        assert len(add_revision.calls) == 1
        assert add_revision.calls[0].kwargs["make_effective"] is True

    @pytest.mark.asyncio
    async def test_existing_revision_is_not_duplicated(
        self, monkeypatch
    ) -> None:
        sid = uuid4()
        add_revision = _make_spy(_new_id)
        monkeypatch.setattr(seed, "create_session", _make_spy())
        monkeypatch.setattr(seed, "add_revision", add_revision)
        session = _FakeSession(
            [SimpleNamespace(id=str(sid)), SimpleNamespace(id=str(uuid4()))]
        )
        starts = datetime(2026, 10, 27, 1, 0, tzinfo=timezone.utc)
        returned = await _ensure_session(
            session,
            ctx=_ctx(),
            batch_id=uuid4(),
            ordinal=1,
            starts_at=starts,
            ends_at=starts + timedelta(hours=2),
        )
        assert returned == sid
        assert add_revision.calls == []


# ---------------------------------------------------------------------------
# _ensure_application_for_participant — status fan-out
# ---------------------------------------------------------------------------


def _run_application(monkeypatch, app_row):
    create = _make_spy(lambda: SimpleNamespace(id=uuid4()))
    submit = _make_spy(lambda: SimpleNamespace(id=uuid4()))
    decide = _make_spy(_new_id)
    monkeypatch.setattr(seed, "create_application", create)
    monkeypatch.setattr(seed, "submit_application", submit)
    monkeypatch.setattr(seed, "decide_application", decide)
    pv_id = uuid4()
    program_id = uuid4()
    session = _FakeSession(
        [app_row, SimpleNamespace(program_version_id=pv_id), _Scalar(str(program_id))]
    )
    return session, create, submit, decide, program_id


class TestEnsureApplicationStatusFanOut:
    @pytest.mark.asyncio
    async def test_approved_application_is_returned_untouched(
        self, monkeypatch
    ) -> None:
        app_id = uuid4()
        session, create, submit, decide, _pid = _run_application(
            monkeypatch, SimpleNamespace(id=str(app_id), status="approved")
        )
        returned = await _ensure_application_for_participant(
            session,
            ctx=_ctx(),
            batch_id=uuid4(),
            participant_id=uuid4(),
            unit_id=uuid4(),
        )
        assert returned == app_id
        assert decide.calls == []
        assert create.calls == []
        assert submit.calls == []

    @pytest.mark.asyncio
    @pytest.mark.parametrize("status", ["submitted", "under_review"])
    async def test_open_application_is_approved(self, monkeypatch, status) -> None:
        app_id = uuid4()
        session, create, _submit, decide, _pid = _run_application(
            monkeypatch, SimpleNamespace(id=str(app_id), status=status)
        )
        returned = await _ensure_application_for_participant(
            session,
            ctx=_ctx(),
            batch_id=uuid4(),
            participant_id=uuid4(),
            unit_id=uuid4(),
        )
        assert returned == app_id
        assert len(decide.calls) == 1
        assert decide.calls[0].kwargs["application_id"] == app_id
        assert decide.calls[0].kwargs["decision"] == "approve"
        assert create.calls == []

    @pytest.mark.asyncio
    async def test_missing_application_is_created_submitted_approved(
        self, monkeypatch
    ) -> None:
        session, create, submit, decide, program_id = _run_application(
            monkeypatch, None
        )
        returned = await _ensure_application_for_participant(
            session,
            ctx=_ctx(),
            batch_id=uuid4(),
            participant_id=uuid4(),
            unit_id=uuid4(),
        )
        assert len(create.calls) == 1
        assert create.calls[0].kwargs["program_id"] == program_id
        assert len(submit.calls) == 1
        assert len(decide.calls) == 1
        assert decide.calls[0].kwargs["decision"] == "approve"
        assert returned is not None


# ---------------------------------------------------------------------------
# _ensure_invoice_with_payment_alloc — receipt flag
# ---------------------------------------------------------------------------


class TestEnsureInvoiceAllocation:
    @pytest.mark.asyncio
    async def test_allocation_issues_a_receipt(self, monkeypatch) -> None:
        invoice_id, installment_id, payment_id = uuid4(), uuid4(), uuid4()
        allocate = _make_spy(
            lambda: (SimpleNamespace(id=uuid4()), SimpleNamespace(id=uuid4()))
        )
        monkeypatch.setattr(
            seed, "create_invoice", _make_spy(lambda: SimpleNamespace(id=invoice_id))
        )
        monkeypatch.setattr(
            seed, "issue_invoice", _make_spy(lambda: SimpleNamespace(id=invoice_id))
        )
        monkeypatch.setattr(
            seed, "submit_payment", _make_spy(lambda: SimpleNamespace(id=uuid4()))
        )
        monkeypatch.setattr(
            seed, "verify_payment", _make_spy(lambda: SimpleNamespace(id=payment_id))
        )
        monkeypatch.setattr(seed, "allocate_funds", allocate)
        session = _FakeSession(
            [None, SimpleNamespace(id=str(installment_id))]
        )
        returned = await _ensure_invoice_with_payment_alloc(
            session,
            ctx=_ctx(),
            unit_id=uuid4(),
            participant_id=uuid4(),
            target_invoice_no="INV-VANETA-0001",
            lines=[{"quantity": 1, "unit_price": 1_000_000}],
            installments=[(1, date(2026, 10, 27), 1_000_000)],
            allocation_amount=1_000_000,
        )
        assert returned == invoice_id
        assert len(allocate.calls) == 1
        kwargs = allocate.calls[0].kwargs
        assert kwargs["issue_receipt"] is True
        assert kwargs["installment_id"] == installment_id
        assert kwargs["payment_id"] == payment_id
        assert kwargs["amount"] == 1_000_000

    @pytest.mark.asyncio
    async def test_allocation_idempotency_key_is_invoice_derived(
        self, monkeypatch
    ) -> None:
        """The idempotency key is the SHA256 of the invoice number truncated
        to 32 hex chars, so a re-run reuses the same allocation.
        """
        import hashlib

        invoice_id, installment_id = uuid4(), uuid4()
        allocate = _make_spy(
            lambda: (SimpleNamespace(id=uuid4()), SimpleNamespace(id=uuid4()))
        )
        monkeypatch.setattr(
            seed, "create_invoice", _make_spy(lambda: SimpleNamespace(id=invoice_id))
        )
        monkeypatch.setattr(
            seed, "issue_invoice", _make_spy(lambda: SimpleNamespace(id=invoice_id))
        )
        monkeypatch.setattr(
            seed, "submit_payment", _make_spy(lambda: SimpleNamespace(id=uuid4()))
        )
        monkeypatch.setattr(
            seed, "verify_payment", _make_spy(lambda: SimpleNamespace(id=uuid4()))
        )
        monkeypatch.setattr(seed, "allocate_funds", allocate)
        session = _FakeSession([None, SimpleNamespace(id=str(installment_id))])
        await _ensure_invoice_with_payment_alloc(
            session,
            ctx=_ctx(),
            unit_id=uuid4(),
            participant_id=uuid4(),
            target_invoice_no="INV-VANETA-0001",
            lines=[],
            installments=[(1, date(2026, 10, 27), 1_000_000)],
            allocation_amount=1_000_000,
        )
        expected = hashlib.sha256(b"INV-VANETA-0001").hexdigest()[:32]
        assert allocate.calls[0].kwargs["idempotency_id"] == UUID(hex=expected)

    @pytest.mark.asyncio
    async def test_unallocated_invoice_skips_the_payment_flow(
        self, monkeypatch
    ) -> None:
        invoice_id = uuid4()
        allocate = _make_spy(
            lambda: (SimpleNamespace(id=uuid4()), SimpleNamespace(id=uuid4()))
        )
        submit = _make_spy(lambda: SimpleNamespace(id=uuid4()))
        monkeypatch.setattr(
            seed, "create_invoice", _make_spy(lambda: SimpleNamespace(id=invoice_id))
        )
        monkeypatch.setattr(
            seed, "issue_invoice", _make_spy(lambda: SimpleNamespace(id=invoice_id))
        )
        monkeypatch.setattr(seed, "submit_payment", submit)
        monkeypatch.setattr(seed, "allocate_funds", allocate)
        session = _FakeSession([None])
        returned = await _ensure_invoice_with_payment_alloc(
            session,
            ctx=_ctx(),
            unit_id=uuid4(),
            participant_id=uuid4(),
            target_invoice_no="INV-VANETA-0002",
            lines=[],
            installments=[(1, date(2026, 10, 27), 600_000)],
            allocation_amount=None,
        )
        assert returned == invoice_id
        assert submit.calls == []
        assert allocate.calls == []


# ---------------------------------------------------------------------------
# main — RLS bypass for the platform-wide bootstrap
# ---------------------------------------------------------------------------


class TestMainRlsBypass:
    @pytest.mark.asyncio
    async def test_bootstrap_bypasses_the_tenant_rls_guc(
        self, monkeypatch
    ) -> None:
        session = _FakeSession()
        set_tenant = _make_spy(None)
        seeded: list[str] = []
        sp_id = uuid4()

        async def _seed_one(spy_session, *, cfg, sp_id):  # noqa: ANN001
            seeded.append(cfg.code)
            return {}

        @asynccontextmanager
        async def _scope():
            yield session

        monkeypatch.setattr(seed, "session_scope", lambda: _scope())
        monkeypatch.setattr(seed, "set_current_tenant", set_tenant)
        monkeypatch.setattr(
            seed, "_ensure_service_principal", _make_spy(lambda: sp_id)
        )
        monkeypatch.setattr(seed, "_seed_one_tenant", _seed_one)
        monkeypatch.setattr(seed, "_bootstrap_operational_run", _make_spy(_new_id))

        await main()

        assert len(set_tenant.calls) == 1
        kwargs = set_tenant.calls[0].kwargs
        assert kwargs["bypass"] is True
        assert kwargs["tenant_id"] == NULL_TENANT
        assert seeded == [cfg.code for cfg in TENANT_CFGS]
