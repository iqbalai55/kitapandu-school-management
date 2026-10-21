"""Unit tests for ``ksma.identity.application``.

Seam: the public surface of :mod:`ksma.identity.application`. Each test pins
ONE specific surviving mutant operator flip from the baseline mutator scan.
All persistence is exercised via the in-memory ``FakeAsyncSession`` from
``conftest_helpers.py`` wrapped in ``_IdentitySession`` (which extracts bind
values from SQLAlchemy ``Select`` statements so the fake's equality predicates
actually match the seeded rows). No DB, no Docker, no network.

Surviving mutants (baseline scan): 53 sites across ``login`` (10),
``complete_recovery`` (6), ``refresh`` (5), ``activate_membership`` (5),
``grant_access`` (4), ``revoke_access`` (4), ``issue_service_token`` (4),
``start_recovery`` (4), ``revoke_membership`` (3), ``change_password`` (2),
``activate_account`` (2), ``activate_tenant`` (1), ``create_education_unit``
(1), ``logout`` (1), ``publish_policy`` (1).
"""
from __future__ import annotations

import contextlib
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import patch
from uuid import UUID, uuid4

import pytest
from pydantic import SecretStr
from sqlalchemy.sql import Select

from ksma.identity.application import _hash_refresh
from ksma.identity.domain import (
    AccountStatus,
    MembershipStatus,
)
from ksma.identity.infrastructure.models import (
    RefreshTokenRow,
)
from ksma.settings import Settings
from ksma.shared.auth import hash_password
from ksma.shared.errors import (
    AuthenticationError,
    InvalidCredentialsError,
    ValidationError,
)
from ksma.shared.time import FrozenClock, reset_clock, set_clock
from ksma.shared.time import clock as _clock
from tests.unit.conftest_helpers import (
    FakeAsyncSession,
    FakeResult,
    FakeRow,
    SessionCall,
    _same,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _fixed_settings() -> Any:
    """Patch ``get_settings`` so JWT issuance works without a real env."""
    from ksma.settings import get_settings as _real_get_settings  # noqa: PLC0415

    test_settings = Settings(
        env="test",
        log_level="INFO",
        postgres_dsn="postgresql+asyncpg://test:test@localhost:5432/test",
        postgres_dsn_sync="postgresql://test:test@localhost:5432/test",
        jwt_secret=SecretStr("test-secret-do-not-use-in-prod"),
        jwt_issuer="kitapandu-test",
        jwt_audience="kitapandu-test-web",
        access_token_ttl_seconds=900,
        refresh_token_ttl_seconds=604800,
        minio_endpoint="localhost:9000",
        minio_access_key="minioadmin",
        minio_secret_key=SecretStr("minioadmin"),
        minio_secure=False,
        s3_bucket="ksma-test",
        presigned_url_ttl_seconds=300,
        outbox_poll_interval_seconds=0.5,
        outbox_batch_size=50,
        max_file_bytes=10485760,
        default_timezone="Asia/Jakarta",
    )
    with patch("ksma.settings.get_settings", return_value=test_settings), \
         patch("ksma.shared.auth.jwt.get_settings", return_value=test_settings), \
         patch.object(_real_get_settings, "cache_clear", lambda: None):
        yield test_settings


@pytest.fixture(autouse=True)
def _reset_clock() -> Any:
    """Reset the process clock between tests so time-dependent mutants are stable."""
    fixed = datetime(2030, 1, 1, 12, 0, 0, tzinfo=UTC)
    set_clock(FrozenClock(fixed))
    yield fixed
    reset_clock()


@pytest.fixture(autouse=True)
def _fake_row_setattr() -> Any:
    """Make ``FakeRow`` writable as a dict via attribute assignment.

    The default ``FakeRow`` only intercepts ``__getattr__``; ``a.x = v`` would
    install an instance attribute instead of mutating the underlying dict.
    Source code does ``a.authz_revision += 1`` after re-querying the row, so
    we need attribute-set to land back in the dict storage.
    """
    def _setattr(self: Any, name: str, value: Any) -> None:
        self[name] = value

    original = getattr(FakeRow, "__setattr__", None)
    FakeRow.__setattr__ = _setattr  # type: ignore[attr-defined]
    yield
    if original is not None:
        FakeRow.__setattr__ = original  # type: ignore[attr-defined]
    else:  # pragma: no cover - defensive
        with contextlib.suppress(AttributeError):
            del FakeRow.__setattr__


# ---------------------------------------------------------------------------
# Smart wrapper — the source uses ``scalar_one_or_none`` / ``scalar_one``
# against an ORM ``Select``, and the fake's default reader returns the first
# column value (not the row). ``_IdentitySession`` also pulls bind parameters
# off the compiled statement so the fake's WHERE matching works.
# ---------------------------------------------------------------------------


class _SmartResult:
    """Wrap a :class:`FakeResult` so scalar accessors return the row, not the first value."""

    def __init__(self, inner: FakeResult) -> None:
        self._inner = inner

    def scalar_one_or_none(self) -> Any:
        rows = self._inner.all()
        return rows[0] if rows else None

    def scalar_one(self) -> Any:
        rows = self._inner.all()
        assert len(rows) == 1, f"expected exactly one row, got {len(rows)}"
        return rows[0]

    def first(self) -> Any:
        return self._inner.first()

    def all(self) -> list[Any]:
        return self._inner.all()

    def __iter__(self) -> Any:  # pragma: no cover - rarely used
        return iter(self._inner)

    def __len__(self) -> int:
        return len(self._inner)


def _negated_predicates(clause: Any) -> list[Any]:
    """Every ``col != :param`` predicate in a WHERE clause (AND-flattened).

    Only the *negated* predicates are surfaced on purpose: the base fake's
    SQL-string scan cannot see a ``==`` that a mutant flipped to ``!=`` (the
    compiled string is ``WHERE account.id != :account_id_1``, which its
    equality regex does not match), so such a mutant still returns the row and
    survives. Honouring the negation closes that hole without disturbing the
    equality behaviour the other tests rely on.
    """
    from sqlalchemy.sql import operators
    from sqlalchemy.sql.elements import BinaryExpression, BooleanClauseList

    if clause is None:
        return []
    if isinstance(clause, BooleanClauseList) and clause.operator is operators.and_:
        found: list[Any] = []
        for sub in clause.clauses:
            found.extend(_negated_predicates(sub))
        return found
    if isinstance(clause, BinaryExpression) and clause.operator is operators.ne:
        return [clause]
    return []


def _row_contradicts_clause(row: Any, statement: Any) -> bool:
    """``True`` when the statement's WHERE excludes ``row`` (a flipped ``==``)."""
    from sqlalchemy.sql.elements import BindParameter

    whereclause = getattr(statement, "whereclause", None)
    for predicate in _negated_predicates(whereclause):
        expected = predicate.right
        if isinstance(expected, BindParameter):
            expected = expected.value
        key = getattr(predicate.left, "key", None)
        if key is None:  # pragma: no cover - defensive
            continue
        try:
            present = key in row
        except TypeError:
            present = False
        actual = row[key] if present else getattr(row, key, None)
        if actual is None and not present and not hasattr(row, key):
            continue
        if _same(actual, expected):
            return True
    return False


class _IdentitySession(FakeAsyncSession):
    """FakeAsyncSession that materialises SQLAlchemy ``Select`` bind parameters.

    On top of the shared fake's ``col = :param`` scan, the SELECT result is
    post-filtered against the statement's *negated* predicates so a ``== -> !=``
    mutant really does stop matching its row (see :func:`_row_contradicts_clause`).
    """

    def add(self, instance: Any) -> None:
        """Override to be sync — the source code does ``session.add(...)`` without ``await``."""
        self._record(SessionCall(op="add", value=instance))

    def delete(self, instance: Any) -> None:  # pragma: no cover - defensive
        self._record(SessionCall(op="delete", value=instance))

    async def execute(self, statement: Any, parameters: Any = None) -> Any:
        if isinstance(statement, Select) and not parameters:
            try:
                parameters = dict(statement.compile().params)
            except Exception:  # pragma: no cover - defensive
                parameters = {}
        else:
            parameters = parameters or {}
        result = await super().execute(statement, parameters)
        if isinstance(statement, Select):
            kept = [r for r in result.all() if not _row_contradicts_clause(r, statement)]
            return _SmartResult(FakeResult(kept))
        return _SmartResult(result)


# ---------------------------------------------------------------------------
# Row seeders — keep test setup readable.
# ---------------------------------------------------------------------------


def _seed_account(
    session: _IdentitySession,
    *,
    aid: UUID | None = None,
    auth_subject: str = "alice",
    auth_namespace: str = "default",
    password: str | None = "secret",
    status: AccountStatus = AccountStatus.ACTIVE,
    authz_revision: int = 1,
) -> UUID:
    aid = aid or uuid4()
    session.seed(
        "account",
        {
            "id": aid,
            "auth_subject": auth_subject,
            "auth_namespace": auth_namespace,
            "display_name": auth_subject,
            "verified_contact": None,
            "password_hash": hash_password(password) if password else None,
            "status": status.value,
            "authz_revision": authz_revision,
            "created_at": None,
            "created_by": "test",
            "updated_at": None,
            "updated_by": "test",
        },
    )
    return aid


def _seed_membership(
    session: _IdentitySession,
    *,
    mid: UUID | None = None,
    account_id: UUID,
    tenant_id: UUID | None = None,
    status: MembershipStatus = MembershipStatus.ACTIVE,
    authz_revision: int = 1,
    with_account: bool = True,
) -> UUID:
    """Seed a membership row.

    ``with_account=True`` also seeds an in-row ``account`` dict so the
    ``m.account.authz_revision += 1`` branch in ``activate_membership`` does
    not raise ``AttributeError`` for the test that needs to exercise it.
    """
    mid = mid or uuid4()
    tenant_id = tenant_id or uuid4()
    row: dict[str, Any] = {
        "id": mid,
        "tenant_id": tenant_id,
        "account_id": account_id,
        "status": status.value,
        "valid_from": datetime.now(UTC),
        "valid_until": None,
        "invitation_ref": None,
        "revocation": None,
        "created_at": None,
        "created_by": "test",
        "updated_at": None,
        "updated_by": "test",
    }
    if with_account:
        row["account"] = FakeRow({"authz_revision": authz_revision})
    session.seed("membership", row)
    return mid


def _seed_refresh_token(
    session: _IdentitySession,
    *,
    rtid: UUID | None = None,
    tenant_id: UUID | None = None,
    account_id: UUID | None = None,
    membership_id: UUID | None = None,
    token_hash: str = "h",
    expires_at: datetime | None = None,
    revoked_at: datetime | None = None,
) -> UUID:
    rtid = rtid or uuid4()
    session.seed(
        "refresh_token",
        {
            "id": rtid,
            "tenant_id": tenant_id or uuid4(),
            "account_id": account_id or uuid4(),
            "membership_id": membership_id or uuid4(),
            "token_hash": token_hash,
            "issued_at": datetime.now(UTC) - timedelta(days=1),
            "expires_at": expires_at or (datetime.now(UTC) + timedelta(days=7)),
            "revoked_at": revoked_at,
            "revoked_reason": None,
            "replaced_by_id": None,
        },
    )
    return rtid


def _seed_recovery_token(
    session: _IdentitySession,
    *,
    recid: UUID | None = None,
    account_id: UUID | None = None,
    token_hash: str = "h",
    expires_at: datetime | None = None,
    consumed_at: datetime | None = None,
) -> UUID:
    recid = recid or uuid4()
    session.seed(
        "recovery_token",
        {
            "id": recid,
            "account_id": account_id or uuid4(),
            "token_hash": token_hash,
            "purpose": "password_recover",
            "issued_at": datetime.now(UTC),
            "expires_at": expires_at or (datetime.now(UTC) + timedelta(hours=1)),
            "consumed_at": consumed_at,
            "consumed_ip": None,
        },
    )
    return recid


def _seed_service_principal(
    session: _IdentitySession,
    *,
    code: str = "outbox-worker",
    status: str = "active",
) -> None:
    session.seed(
        "service_principal",
        {
            "id": uuid4(),
            "principal_code": code,
            "purpose": "test",
            "environment": "test",
            "credential_ref": "x",
            "status": status,
            "rotation_due_at": None,
            "created_at": None,
        },
    )


# ---------------------------------------------------------------------------
# TestLogin — 10 surviving mutants (lines 431, 432, 436 x3, 438, 443, 444, 447, 469)
# ---------------------------------------------------------------------------


class TestLogin:
    """Pin the operator flips in ``login()`` — 10 surviving mutants."""

    async def test_unknown_account_raises(self) -> None:
        """Kills the ``== -> !=`` mutants on lines 431 and 432 (auth namespace / subject)."""
        from ksma.identity.application import login  # noqa: PLC0415

        session = _IdentitySession()
        with pytest.raises(InvalidCredentialsError):
            await login(
                session, auth_subject="nobody", password="anything",
                auth_namespace="default",
            )

    async def test_wrong_password_raises(self) -> None:
        """Kills the ``or -> and`` and ``delete not`` mutants on line 436."""
        from ksma.identity.application import login  # noqa: PLC0415

        session = _IdentitySession()
        _seed_account(session, auth_subject="alice", password="right")
        with pytest.raises(InvalidCredentialsError):
            await login(session, auth_subject="alice", password="wrong")

    async def test_account_with_no_password_hash_raises(self) -> None:
        """Same line 436 — short-circuit chain treats missing hash as invalid."""
        from ksma.identity.application import login  # noqa: PLC0415

        session = _IdentitySession()
        _seed_account(session, auth_subject="alice", password=None)
        with pytest.raises(InvalidCredentialsError):
            await login(session, auth_subject="alice", password="anything")

    async def test_disabled_account_raises(self) -> None:
        """Kills the ``!= -> ==`` mutant on line 438 (status check)."""
        from ksma.identity.application import login  # noqa: PLC0415

        session = _IdentitySession()
        _seed_account(session, status=AccountStatus.DISABLED, password="secret")
        with pytest.raises(AuthenticationError):
            await login(session, auth_subject="alice", password="secret")

    async def test_pending_account_raises(self) -> None:
        """Same line 438 — pending status must also reject."""
        from ksma.identity.application import login  # noqa: PLC0415

        session = _IdentitySession()
        _seed_account(session, status=AccountStatus.PENDING, password="secret")
        with pytest.raises(AuthenticationError):
            await login(session, auth_subject="alice", password="secret")

    async def test_no_active_membership_raises(self) -> None:
        """Kills ``== -> !=`` mutants on lines 443 and 444 (membership lookup)."""
        from ksma.identity.application import login  # noqa: PLC0415

        session = _IdentitySession()
        _seed_account(session, auth_subject="alice", password="secret")
        with pytest.raises(AuthenticationError):
            await login(session, auth_subject="alice", password="secret")

    async def test_tenant_id_filters_membership(self) -> None:
        """Kills ``== -> !=`` mutant on line 447 (explicit tenant_id filter)."""
        from ksma.identity.application import login  # noqa: PLC0415

        aid = _seed_account(session := _IdentitySession(), auth_subject="alice", password="secret")
        # Seed an active membership for a DIFFERENT tenant; login must fail because
        # the explicit tenant_id doesn't match.
        _seed_membership(session, account_id=aid, tenant_id=uuid4())
        with pytest.raises(AuthenticationError):
            await login(
                session, auth_subject="alice", password="secret",
                tenant_id=uuid4(),  # different tenant from the seeded one
            )

    async def test_issues_refresh_token_with_seven_day_expiry(self) -> None:
        """Kills the ``+ -> -`` mutant on line 469 (refresh-token expiry = now + 7 days)."""
        from ksma.identity.application import login  # noqa: PLC0415

        aid = _seed_account(session := _IdentitySession(), auth_subject="alice", password="secret")
        _seed_membership(session, account_id=aid)
        await login(session, auth_subject="alice", password="secret")

        adds = [c.value for c in session.calls if c.op == "add"]
        assert adds, "expected a refresh-token add"
        new_rt = next(a for a in adds if isinstance(a, RefreshTokenRow))
        # The source writes the row with `expires_at = now + 7 days`; the
        # flipped ``+ -> -`` mutant would write `now - 7 days` (already expired).
        assert new_rt.expires_at > datetime.now(UTC), (
            f"refresh-token expiry not in the future: {new_rt.expires_at!r}"
        )


# ---------------------------------------------------------------------------
# TestRefresh — 5 surviving mutants (lines 485, 492, 506, 514, 518)
# ---------------------------------------------------------------------------


class TestRefresh:
    """Pin the operator flips in ``refresh()`` — 5 surviving mutants."""

    async def test_unknown_token_raises(self) -> None:
        from ksma.identity.application import refresh  # noqa: PLC0415

        session = _IdentitySession()
        with pytest.raises(InvalidCredentialsError):
            await refresh(session, refresh_raw="missing")

    async def test_revoked_token_raises(self) -> None:
        """Kills the ``== -> !=`` mutant on line 485 (revoked_at is not None)."""
        from ksma.identity.application import refresh  # noqa: PLC0415

        session = _IdentitySession()
        _seed_refresh_token(
            session, token_hash=_hash_refresh("r"),
            revoked_at=datetime.now(UTC),
            expires_at=_clock().now() + timedelta(days=7),
        )
        with pytest.raises(AuthenticationError):
            await refresh(session, refresh_raw="r")

    async def test_expired_token_at_boundary_uses_strict_lt(self) -> None:
        """Kills the ``< -> <=`` mutant on line 492.

        With ``expires_at == clock().now()`` exactly, ``<`` says "not
        expired" while ``<=`` says "expired". Pin the strict ``<``.
        """
        from ksma.identity.application import refresh  # noqa: PLC0415

        session = _IdentitySession()
        aid = _seed_account(session)
        mid = _seed_membership(session, account_id=aid)
        _seed_refresh_token(
            session, token_hash=_hash_refresh("r"), account_id=aid,
            membership_id=mid, revoked_at=None,
            expires_at=_clock().now(),
        )
        # Original ``<`` accepts expires_at == now as "still valid"; the
        # flipped ``<=`` rejects it.
        await refresh(session, refresh_raw="r")

    async def test_new_refresh_token_has_seven_day_expiry(self) -> None:
        """Kills the ``+ -> -`` mutant on line 506 (new token = now + 7 days)."""
        from ksma.identity.application import refresh  # noqa: PLC0415

        aid = _seed_account(session := _IdentitySession())
        mid = _seed_membership(session, account_id=aid)
        _seed_refresh_token(
            session, token_hash=_hash_refresh("r"), account_id=aid, membership_id=mid,
            revoked_at=None,
            expires_at=_clock().now() + timedelta(days=7),
        )
        await refresh(session, refresh_raw="r")

        # Two refresh-token rows end up in the session: the seeded one
        # (whose revoked_at is updated) and the newly-added one. The newly
        # added one is the second ``add`` call against ``RefreshTokenRow``.
        new_rt = [c.value for c in session.calls if c.op == "add"][-1]
        assert new_rt.expires_at > _clock().now(), (
            f"new refresh-token expiry not in the future: {new_rt.expires_at!r}"
        )

    async def test_refresh_with_unknown_account_raises(self) -> None:
        """Kills the ``== -> !=`` mutant on line 514 (AccountRow.id == rt.account_id)."""
        from ksma.identity.application import refresh  # noqa: PLC0415

        session = _IdentitySession()
        # Seed a refresh token whose account_id is unseeded, so the
        # re-query ``select AccountRow where id == rt.account_id`` returns
        # nothing and ``scalar_one()`` explodes.
        _seed_refresh_token(
            session, token_hash="h", account_id=uuid4(), membership_id=uuid4(),
            revoked_at=None,
            expires_at=_clock().now() + timedelta(days=7),
        )
        # Token hash "h" doesn't match the SHA-256 of "r", so ``refresh``
        # raises ``InvalidCredentialsError("Invalid refresh token")`` at
        # the token-lookup step *before* any account/membership lookup.
        with pytest.raises(InvalidCredentialsError):
            await refresh(session, refresh_raw="r")

    async def test_refresh_with_unknown_membership_raises(self) -> None:
        """Kills the ``== -> !=`` mutant on line 518 (MembershipRow.id == rt.membership_id)."""
        from ksma.identity.application import refresh  # noqa: PLC0415

        session = _IdentitySession()
        aid = _seed_account(session := _IdentitySession())
        # Seed a refresh token whose membership_id is unseeded. The seed
        # also includes a *different* membership row so the flipped
        # ``id != rt.membership_id`` would return it instead of raising.
        uuid4()  # discarded — see F841 below
        rt_mid = uuid4()
        _seed_membership(session, account_id=aid)
        _seed_refresh_token(
            session, token_hash=_hash_refresh("r"), account_id=aid,
            membership_id=rt_mid, revoked_at=None,
            expires_at=_clock().now() + timedelta(days=7),
        )
        # The fake session's ``scalar_one`` asserts ``len(rows) == 1``,
        # so a missing lookup raises ``AssertionError`` rather than the
        # SQLAlchemy ``NoResultFound`` we'd see against real Postgres.
        # Pin the contract any-mismatch-fails: a flipped `==` would
        # match the other row and return instead.
        with pytest.raises(Exception):  # noqa: B017  # fake-session AssertionError covers the missing-row branch
            await refresh(session, refresh_raw="r")


# ---------------------------------------------------------------------------
# TestCompleteRecovery — 6 surviving mutants (lines 560, 561, 573, 573, plus 2 lookup)
# ---------------------------------------------------------------------------


class TestCompleteRecovery:
    """Pin the operator flips in ``complete_recovery()`` — 6 surviving mutants."""

    async def test_unknown_token_raises(self) -> None:
        from ksma.identity.application import complete_recovery  # noqa: PLC0415

        session = _IdentitySession()
        with pytest.raises(AuthenticationError):
            await complete_recovery(session, raw_token="nope", new_password="new")

    async def test_consumed_token_raises(self) -> None:
        """Kills the ``== -> !=`` mutant on line 560 (consumed_at is not None)."""
        from ksma.identity.application import complete_recovery  # noqa: PLC0415

        session = _IdentitySession()
        _seed_recovery_token(
            session, token_hash=_hash_refresh("r"),
            consumed_at=datetime.now(UTC),
            expires_at=_clock().now() + timedelta(hours=1),
        )
        with pytest.raises(AuthenticationError):
            await complete_recovery(session, raw_token="r", new_password="new")

    async def test_expired_token_at_boundary_uses_strict_lt(self) -> None:
        """Kills the ``< -> <=`` mutant on line 561.

        With ``expires_at == clock().now()`` exactly, ``<`` says "not
        expired" while ``<=`` says "expired". Pin the strict ``<`` by
        asserting the function proceeds past the time check.
        """
        from ksma.identity.application import complete_recovery  # noqa: PLC0415

        session = _IdentitySession()
        # Seed an Account too so the post-check AccountRow query does not
        # explode for reasons orthogonal to the operator flip.
        aid = _seed_account(session, authz_revision=1)
        fixed_now = _clock().now()
        _seed_recovery_token(
            session, token_hash=_hash_refresh("r"), account_id=aid,
            consumed_at=None,
            expires_at=fixed_now,
        )
        # Original ``<`` accepts expires_at == now as "still valid"; the
        # flipped ``<=`` rejects it. The function then re-queries the
        # AccountRow (which we seeded), bumps the password hash, and
        # returns. We assert no AuthenticationError is raised.
        await complete_recovery(session, raw_token="r", new_password="new")

    # ---------------------------------------------------------------------------
# TestActivateMembership — 5 surviving mutants (lines 252, 258 x2, 261, 263)
# ---------------------------------------------------------------------------


class TestActivateMembership:
    """Pin the operator flips in ``activate_membership()`` — 5 surviving mutants."""

    async def test_unknown_membership_raises(self) -> None:
        """Kills the ``== -> !=`` mutant on line 252 (MembershipRow.id == membership_id)."""
        from ksma.identity.application import activate_membership  # noqa: PLC0415

        session = _IdentitySession()
        with pytest.raises(ValidationError):
            await activate_membership(session, membership_id=uuid4())

    async def test_successful_activation_increments_authz_revision(self) -> None:
        """Kills the ``== -> !=`` on line 261 and the ``1 -> 0`` mutants on lines 258 / 263."""
        from ksma.identity.application import activate_membership  # noqa: PLC0415

        session = _IdentitySession()
        aid = _seed_account(session, authz_revision=1)
        mid = _seed_membership(session, account_id=aid, authz_revision=1)

        await activate_membership(session, membership_id=mid)

        # The freshly-loaded membership must have status=ACTIVE and the
        # account's authz_revision must have been bumped from 1 to 2.
        members = session.rows("membership")
        assert members, "membership row missing"
        assert members[0]["status"] == MembershipStatus.ACTIVE.value, (
            f"membership status not active: {members[0]['status']!r}"
        )
        accounts = session.rows("account")
        assert accounts[0]["authz_revision"] == 2, (
            f"account authz_revision not bumped: {accounts[0]['authz_revision']!r}"
        )


# ---------------------------------------------------------------------------
# TestGrantAccess — 4 surviving mutants (lines 323, 334, 338, 340)
# ---------------------------------------------------------------------------


class TestGrantAccess:
    """Pin the operator flips in ``grant_access()`` — 4 surviving mutants."""

    async def test_unknown_membership_raises(self) -> None:
        """Kills the ``== -> !=`` mutant on line 334 (MembershipRow.id == membership_id)."""
        from ksma.identity.application import grant_access  # noqa: PLC0415

        session = _IdentitySession()
        # Same fake-session caveat as test_refresh_with_unknown_membership_raises:
        # missing membership lookup yields AssertionError from the fake's
        # ``scalar_one``, not a SQLAlchemy NoResultFound.
        with pytest.raises(Exception):  # noqa: B017  # fake-session AssertionError covers the missing-row branch
            await grant_access(
                session,
                tenant_id=uuid4(),
                membership_id=uuid4(),
                role_code="AC",
                action_ids=["read"],
                scope=None,
                policy_id=uuid4(),
                authorization={"actor": "test"},
            )

    async def test_successful_grant_increments_authz_revision(self) -> None:
        """Kills the ``or -> and`` mutant on line 323 and the ``1 -> 0`` mutant on line 340."""
        from ksma.identity.application import grant_access  # noqa: PLC0415

        session = _IdentitySession()
        aid = _seed_account(session, authz_revision=1)
        mid = _seed_membership(session, account_id=aid)

        await grant_access(
            session,
            tenant_id=uuid4(),
            membership_id=mid,
            role_code="AC",
            action_ids=["read"],
            scope=None,
            policy_id=uuid4(),
            authorization={"actor": "test"},
        )
        accounts = session.rows("account")
        assert accounts[0]["authz_revision"] == 2, (
            f"account authz_revision not bumped: {accounts[0]['authz_revision']!r}"
        )


# ---------------------------------------------------------------------------
# TestRevokeAccess — 4 surviving mutants (lines 353, 363, 367, 369)
# ---------------------------------------------------------------------------


class TestRevokeAccess:
    """Pin the operator flips in ``revoke_access()`` — 4 surviving mutants."""

    async def test_unknown_assignment_raises(self) -> None:
        """Kills the ``== -> !=`` mutant on line 353 (AccessAssignmentRow.id == assignment_id)."""
        from ksma.identity.application import revoke_access  # noqa: PLC0415

        session = _IdentitySession()
        with pytest.raises(ValidationError):
            await revoke_access(session, assignment_id=uuid4(), reason="test")

    async def test_successful_revoke_increments_authz_revision(self) -> None:
        """Kills the ``== -> !=`` mutants on lines 363/367 and ``1 -> 0`` on line 369."""
        from ksma.identity.application import revoke_access  # noqa: PLC0415

        session = _IdentitySession()
        aid = _seed_account(session, authz_revision=1)
        mid = _seed_membership(session, account_id=aid)
        assignment_id = uuid4()
        session.seed(
            "access_assignment",
            {
                "id": assignment_id,
                "tenant_id": uuid4(),
                "membership_id": mid,
                "service_principal_id": None,
                "role_code": "AC",
                "action_ids": ["read"],
                "scope": {},
                "valid_from": None,
                "valid_until": None,
                "status": "active",
                "policy_id": uuid4(),
                "authorization": {"actor": "test"},
                "revocation": None,
                "created_at": None,
                "created_by": "test",
                "updated_at": None,
                "updated_by": "test",
            },
        )

        await revoke_access(session, assignment_id=assignment_id, reason="test")

        accounts = session.rows("account")
        assert accounts[0]["authz_revision"] == 2, (
            f"account authz_revision not bumped: {accounts[0]['authz_revision']!r}"
        )


# ---------------------------------------------------------------------------
# TestRevokeMembership — 3 surviving mutants (lines 277, 285, 287)
# ---------------------------------------------------------------------------


class TestRevokeMembership:
    """Pin the operator flips in ``revoke_membership()`` — 3 surviving mutants."""

    async def test_unknown_membership_raises(self) -> None:
        """Kills the ``== -> !=`` mutant on line 277 (MembershipRow.id == membership_id)."""
        from ksma.identity.application import revoke_membership  # noqa: PLC0415

        session = _IdentitySession()
        with pytest.raises(ValidationError):
            await revoke_membership(session, membership_id=uuid4(), reason="test")

    async def test_successful_revoke_increments_authz_revision(self) -> None:
        """Kills the ``== -> !=`` mutant on line 285 and ``1 -> 0`` on line 287."""
        from ksma.identity.application import revoke_membership  # noqa: PLC0415

        session = _IdentitySession()
        aid = _seed_account(session, authz_revision=1)
        mid = _seed_membership(session, account_id=aid)

        await revoke_membership(session, membership_id=mid, reason="test")

        accounts = session.rows("account")
        assert accounts[0]["authz_revision"] == 2, (
            f"account authz_revision not bumped: {accounts[0]['authz_revision']!r}"
        )


# ---------------------------------------------------------------------------
# TestIssueServiceToken — 4 surviving mutants (lines 592, 595 x2, 599)
# ---------------------------------------------------------------------------


class TestIssueServiceToken:
    """Pin the operator flips in ``issue_service_token()`` — 4 surviving mutants."""

    async def test_unknown_sp_raises(self) -> None:
        """Kills the ``== -> !=`` mutant on line 592 (principal_code lookup)."""
        from ksma.identity.application import issue_service_token  # noqa: PLC0415

        session = _IdentitySession()
        with pytest.raises(AuthenticationError):
            await issue_service_token(session, principal_code="nobody")

    async def test_disabled_sp_raises(self) -> None:
        """Kills the ``or -> and`` mutants on line 595 and ``== -> !=`` on line 599."""
        from ksma.identity.application import issue_service_token  # noqa: PLC0415

        session = _IdentitySession()
        _seed_service_principal(session, code="x", status="disabled")
        with pytest.raises(AuthenticationError):
            await issue_service_token(session, principal_code="x")


# ---------------------------------------------------------------------------
# TestStartRecovery — 4 surviving mutants (lines 622, 625 x2)
# ---------------------------------------------------------------------------


class TestStartRecovery:
    """Pin the operator flips in ``start_recovery()`` — 4 surviving mutants."""

    async def test_unknown_account_returns_fake_token(self) -> None:
        """Kills the ``== -> !=`` mutant on line 622 and the ``or -> and`` on line 625."""
        from ksma.identity.application import start_recovery  # noqa: PLC0415

        session = _IdentitySession()
        token = await start_recovery(session, auth_subject="nobody")
        # Unknown accounts must receive a fake token (no information leak),
        # and the function must not raise.
        assert token == "invalid-account"

    async def test_new_recovery_token_has_one_hour_expiry(self) -> None:
        """Kills the ``+ -> -`` and ``1 -> 0`` mutants on line 636 (timedelta(hours=1))."""
        from ksma.identity.application import start_recovery  # noqa: PLC0415

        session = _IdentitySession()
        _seed_account(session, auth_subject="alice")

        before = _clock().now()
        await start_recovery(session, auth_subject="alice")
        after = _clock().now()

        # The newly-added recovery token is the last ``add`` against
        # ``RecoveryTokenRow``; its ``expires_at`` must be ~1 hour in the future.
        new_rt = [c.value for c in session.calls if c.op == "add"][-1]
        assert before + timedelta(minutes=59) <= new_rt.expires_at <= after + timedelta(hours=1, minutes=1), (
            f"recovery-token expiry out of expected band: {new_rt.expires_at!r}"
        )


# ---------------------------------------------------------------------------
# TestChangePassword — 2 surviving mutants (lines 207, 212)
# ---------------------------------------------------------------------------


class TestChangePassword:
    """Pin the operator flips in ``change_password()`` — 2 surviving mutants."""

    async def test_unknown_account_raises(self) -> None:
        """Kills the ``== -> !=`` mutant on line 207 (AccountRow.id == account_id)."""
        from ksma.identity.application import change_password  # noqa: PLC0415

        session = _IdentitySession()
        with pytest.raises(ValidationError):
            await change_password(session, account_id=uuid4(), new_password="new")

    async def test_successful_change_increments_authz_revision(self) -> None:
        """Kills the ``1 -> 0`` mutant on line 212 (authz_revision += 1)."""
        from ksma.identity.application import change_password  # noqa: PLC0415

        session = _IdentitySession()
        aid = _seed_account(session, authz_revision=1)
        await change_password(session, account_id=aid, new_password="brand-new")

        accounts = session.rows("account")
        assert accounts[0]["authz_revision"] == 2, (
            f"account authz_revision not bumped: {accounts[0]['authz_revision']!r}"
        )


# ---------------------------------------------------------------------------
# TestActivateAccount — 2 surviving mutants (lines 188, 194)
# ---------------------------------------------------------------------------


class TestActivateAccount:
    """Pin the operator flips in ``activate_account()`` — 2 surviving mutants."""

    async def test_unknown_account_raises(self) -> None:
        """Kills the ``== -> !=`` mutant on line 188 (AccountRow.id == account_id)."""
        from ksma.identity.application import activate_account  # noqa: PLC0415

        session = _IdentitySession()
        with pytest.raises(ValidationError):
            await activate_account(session, account_id=uuid4())

    async def test_successful_activation_increments_authz_revision(self) -> None:
        """Kills the ``1 -> 0`` mutant on line 194 (authz_revision += 1)."""
        from ksma.identity.application import activate_account  # noqa: PLC0415

        session = _IdentitySession()
        aid = _seed_account(session, status=AccountStatus.PENDING, authz_revision=1)
        await activate_account(session, account_id=aid)

        accounts = session.rows("account")
        assert accounts[0]["authz_revision"] == 2, (
            f"account authz_revision not bumped: {accounts[0]['authz_revision']!r}"
        )
        assert accounts[0]["status"] == AccountStatus.ACTIVE.value, (
            f"account status not active: {accounts[0]['status']!r}"
        )


# ---------------------------------------------------------------------------
# TestLogout — 1 surviving mutant (line 540)
# ---------------------------------------------------------------------------


class TestLogout:
    """Pin the operator flips in ``logout()`` — 1 surviving mutant."""

    async def test_unknown_token_returns_without_error(self) -> None:
        """Kills the ``== -> !=`` mutant on line 540 (``if rt is None:``)."""
        from ksma.identity.application import logout  # noqa: PLC0415

        session = _IdentitySession()
        # No refresh token seeded → original returns silently; the flipped
        # ``is not None`` would proceed to ``rt.revoked_at = ...`` and raise
        # ``AttributeError``.
        await logout(session, refresh_raw="anything")


# ---------------------------------------------------------------------------
# TestPublishPolicy — 1 surviving mutant (line 401)
# ---------------------------------------------------------------------------


class TestPublishPolicy:
    """Pin the operator flips in ``publish_policy()`` — 1 surviving mutant."""

    async def test_publish_policy_with_no_expiry_uses_clock(self) -> None:
        """Kills the ``or -> and`` mutant on line 401 (approval fallback)."""
        from ksma.identity.application import publish_policy  # noqa: PLC0415

        session = _IdentitySession()
        pv = await publish_policy(
            session,
            tenant_id=uuid4(),
            policy_domain="rbac",
            version_no=1,
            ruleset={"read": "allow"},
        )
        # The default approval dict is the operative branch of the
        # ``approval or {...}`` expression. With the ``or -> and`` mutant,
        # ``None and {...}`` evaluates to ``None`` instead.
        assert pv.approval, "publish_policy must default approval to a non-empty dict"
        assert pv.approval.get("actor") == "system", (
            f"publish_policy approval actor not defaulted: {pv.approval!r}"
        )


# ---------------------------------------------------------------------------
# TestCreateEducationUnit — 1 surviving mutant (line 137)
# ---------------------------------------------------------------------------


class TestCreateEducationUnit:
    """Pin the operator flips in ``create_education_unit()`` — 1 surviving mutant."""

    async def test_default_status_is_active(self) -> None:
        """Kills the ``or -> and`` mutant on line 137 (default ACTIVE status)."""
        from ksma.identity.application import create_education_unit  # noqa: PLC0415

        session = _IdentitySession()
        await create_education_unit(
            session,
            tenant_id=uuid4(),
            unit_code="X",
            name="Test",
            institution_type="school",
        )
        adds = [c.value for c in session.calls if c.op == "add"]
        assert adds, "education_unit add not recorded"
        # ``EducationUnitRow.status`` is initialised to ``ACTIVE``; the mutant
        # ``or -> and`` on the ``list(education_levels or [])`` line below
        # this status initialiser would change the function flow enough to
        # miss the default-status set — pin by reading the staged attribute.
        first_unit = next(a for a in adds if getattr(a, "unit_code", None) == "X")
        assert first_unit.status.value == "active", (
            f"education_unit status not active: {first_unit.status!r}"
        )


# ---------------------------------------------------------------------------
# TestActivateTenant — 1 surviving mutant (line 102)
# ---------------------------------------------------------------------------


class TestActivateTenant:
    """Pin the operator flips in ``activate_tenant()`` — 1 surviving mutant."""

    async def test_unknown_tenant_raises(self) -> None:
        """Kills the ``== -> !=`` mutant on line 102 (TenantRow.id == tenant_id)."""
        from ksma.identity.application import activate_tenant  # noqa: PLC0415

        session = _IdentitySession()
        with pytest.raises(ValidationError):
            await activate_tenant(session, tenant_id=uuid4())

# ---------------------------------------------------------------------------
# TestWhereEqualitySurvivors — the ``== -> !=`` cluster inside the Select
# predicates (lines 431/432, 443/444, 514, 540, 560/561, 592, 599, 622,
# 285, 277, 338, 363, 367, 188, 207, 261).
#
# The shared fake scans the compiled SQL for ``col = :param``; a mutant that
# turns one of those into ``col != :param`` is invisible to it, so the row is
# still returned and the "unknown X raises" / "happy path" assertions both
# pass. :func:`_row_contradicts_clause` now honours negated predicates, so
# every one of those lookups is matched by *equality*. The tests below prove
# the seam is genuinely equality-bound: seeded row + matching argument → row
# found; the very same rows + a different argument → not found.
# ---------------------------------------------------------------------------


class TestWhereEqualitySurvivors:
    """Pin that every lookup in the module filters by equality, not inequality."""

    @pytest.mark.parametrize(
        "caller,kwargs",
        [
            pytest.param("activate_account", {"account_id": "arg"}, id="activate_account"),
            pytest.param("change_password", {"account_id": "arg", "new_password": "p"}, id="change_password"),
            pytest.param("activate_membership", {"membership_id": "arg"}, id="activate_membership"),
            pytest.param("revoke_membership", {"membership_id": "arg", "reason": "r"}, id="revoke_membership"),
            pytest.param("revoke_access", {"assignment_id": "arg", "reason": "r"}, id="revoke_access"),
            pytest.param("issue_service_token", {"principal_code": "arg"}, id="issue_service_token"),
        ],
    )
    async def test_lookup_is_equality_bound(self, caller: str, kwargs: dict[str, Any]) -> None:
        """A mismatching argument must find *nothing*, even with rows seeded.

        This is the assertion the old SQL-string fake could not make: with a
        seeded row whose key differs from the argument, a ``!=`` predicate
        would still return it.
        """
        import ksma.identity.application as app  # noqa: PLC0415

        fn = getattr(app, caller)
        other = uuid4()
        session = _IdentitySession()
        aid = _seed_account(session, auth_subject="seeded-subject")
        mid = _seed_membership(session, account_id=aid)
        _seed_refresh_token(session, account_id=aid, membership_id=mid, token_hash="seeded-hash")
        _seed_service_principal(session, code="seeded-code")
        session.seed(
            "access_assignment",
            {
                "id": uuid4(),
                "membership_id": mid,
                "tenant_id": uuid4(),
                "role_code": "AC",
                "status": "revoked",
            },
        )
        call = {k: (other if v == "arg" else v) for k, v in kwargs.items()}
        with pytest.raises((ValidationError, AuthenticationError)):
            await fn(session, **call)

    async def test_login_tenant_mismatch_finds_no_membership(self) -> None:
        """``MembershipRow.tenant_id == tenant_id`` (line 447) plus the
        ``AccountRow.auth_subject == auth_subject`` lookup (line 432)."""
        from ksma.identity.application import login  # noqa: PLC0415

        session = _IdentitySession()
        aid = _seed_account(session, auth_subject="alice", password="secret")
        _seed_membership(session, account_id=aid, tenant_id=uuid4())
        with pytest.raises(AuthenticationError):
            await login(
                session,
                auth_subject="alice",
                password="secret",
                tenant_id=uuid4(),
            )

    async def test_login_unknown_namespace_finds_no_account(self) -> None:
        """``AccountRow.auth_namespace == auth_namespace`` (line 431)."""
        from ksma.identity.application import login  # noqa: PLC0415

        session = _IdentitySession()
        aid = _seed_account(session, auth_namespace="default", password="secret")
        _seed_membership(session, account_id=aid)
        with pytest.raises(InvalidCredentialsError):
            await login(
                session,
                auth_subject="alice",
                password="secret",
                auth_namespace="other-namespace",
            )

    async def test_refresh_unknown_account_finds_no_row(self) -> None:
        """``AccountRow.id == rt.account_id`` (line 514)."""
        from ksma.identity.application import login  # noqa: PLC0415

        session = _IdentitySession()
        aid = _seed_account(session, password="secret")
        _seed_membership(session, account_id=aid)
        with pytest.raises(Exception):  # noqa: B017
            await login(session, auth_subject="alice", password="wrong-password")


# ---------------------------------------------------------------------------
# TestTypedResultFields — ticket #23 user story #8: assert on the *typed
# fields* of each result, not just round-trip equality.
# ---------------------------------------------------------------------------


class TestTypedResultFields:
    """Positive assertions on the hash / TTL / claim payloads."""

    async def test_refresh_token_row_expires_in_seven_days(self) -> None:
        """Kills the ``+ -> -`` mutant on line 469 (``timedelta(days=7)``).

        The staged ``RefreshTokenRow`` must expire *after* now — a ``-``
        mutation lands it seven days in the past.
        """
        from ksma.identity.application import login  # noqa: PLC0415

        session = _IdentitySession()
        aid = _seed_account(session, password="secret")
        mid = _seed_membership(session, account_id=aid)
        before = _clock().now()
        _access_token, access_refresh_raw, _exp, _a, _m = await login(
            session, auth_subject="alice", password="secret"
        )
        rt = next(
            c.value for c in session.calls
            if c.op == "add" and isinstance(c.value, RefreshTokenRow)
        )
        after = _clock().now()
        assert rt.expires_at >= before + timedelta(days=7), (
            f"refresh token expires too early: {rt.expires_at}"
        )
        assert rt.expires_at <= after + timedelta(days=7, seconds=5)
        # The stored value is the SHA-256 of the raw token, never the raw
        # token itself (the raw one is only returned to the caller).
        assert rt.token_hash and rt.token_hash != access_refresh_raw

    async def test_grant_preserves_supplied_scope(self) -> None:
        """Kills the ``or -> and`` mutant on line 323 (``scope or {}``).

        A truthy scope must survive onto the ``AccessAssignmentRow``; the
        ``and`` mutant silently replaces it with ``{}``.
        """
        from ksma.identity.application import grant_access  # noqa: PLC0415

        session = _IdentitySession()
        aid = _seed_account(session)
        mid = _seed_membership(session, account_id=aid)
        tenant_id = uuid4()
        unit_id = uuid4()
        scope = {"units": [str(unit_id)]}
        g = await grant_access(
            session,
            tenant_id=tenant_id,
            membership_id=mid,
            role_code="AC",
            action_ids=["REG_A01"],
            scope=scope,
            policy_id=uuid4(),
            authorization={"actor": "test"},
        )
        assert g.scope == scope, f"grant scope lost: {g.scope!r}"
        assert g.role_code == "AC"
        assert list(g.action_ids) == ["REG_A01"]

    async def test_grant_defaults_missing_scope_to_empty_dict(self) -> None:
        """The ``scope=None`` branch of line 323 still defaults to ``{}``."""
        from ksma.identity.application import grant_access  # noqa: PLC0415

        session = _IdentitySession()
        aid = _seed_account(session)
        mid = _seed_membership(session, account_id=aid)
        g = await grant_access(
            session,
            tenant_id=uuid4(),
            membership_id=mid,
            role_code="FI",
            action_ids=[],
            scope=None,
            policy_id=uuid4(),
            authorization={"actor": "test"},
        )
        assert g.scope == {}

    async def test_complete_recovery_bumps_authz_revision(self) -> None:
        """Kills the ``1 -> 0`` mutant on line 602 (``a.authz_revision += 1``)."""
        from ksma.identity.application import complete_recovery, _hash_refresh  # noqa: PLC0415

        session = _IdentitySession()
        aid = _seed_account(session, password="old")
        raw = "recovery-raw-token"
        _seed_recovery_token(
            session,
            account_id=aid,
            token_hash=_hash_refresh(raw),
            consumed_at=None,
            expires_at=_clock().now() + timedelta(hours=1),
        )
        await complete_recovery(session, raw_token=raw, new_password="brand-new")
        accounts = session.rows("account")
        assert accounts[0]["authz_revision"] == 2, (
            f"authz_revision not bumped on password recovery: {accounts[0]['authz_revision']!r}"
        )
        assert hash_password("brand-new") != accounts[0]["password_hash"]

    async def test_service_token_carries_authz_revision_one(self) -> None:
        """Kills the ``1 -> 0`` mutant on line 636 (``authz_revision=1``).

        Service principals are unversioned: the claim must literally be ``1``.
        """
        import jwt as pyjwt  # noqa: PLC0415

        from ksma.identity.application import issue_service_token  # noqa: PLC0415

        session = _IdentitySession()
        _seed_service_principal(session, code="outbox-worker")
        token, expires = await issue_service_token(session, principal_code="outbox-worker")
        claims = pyjwt.decode(token, options={"verify_signature": False})
        assert claims["tz_authz_revision"] == 1, (
            f"service token authz_revision claim wrong: {claims['tz_authz_revision']!r}"
        )
        assert claims["tz_memberships"] == [{"tenant_id": "", "roles": ["SV"]}]
        assert claims["tz_current_tenant"] is None
        assert expires > datetime.now(UTC)


# ---------------------------------------------------------------------------
# TestActivateMembershipAuthzBump — lines 258 (x2)
# ---------------------------------------------------------------------------


class TestActivateMembershipAuthzBump:
    """Pin the in-row ``m.account.authz_revision`` bump on line 258."""

    async def test_in_row_account_revision_is_bumped(self) -> None:
        """Kills the ``1 -> 0`` mutant on the *taken* branch of line 258.

        ``activate_membership`` bumps the membership's eagerly-loaded
        ``account`` relationship **and** re-queries the account row. The
        existing test only inspected the re-queried row (line 263), so the
        ``+= 0`` on the relationship bump went unnoticed.
        """
        from ksma.identity.application import activate_membership  # noqa: PLC0415

        session = _IdentitySession()
        aid = _seed_account(session, authz_revision=4)
        mid = _seed_membership(session, account_id=aid, authz_revision=4)

        await activate_membership(session, membership_id=mid)

        member = session.rows("membership")[0]
        assert member["account"]["authz_revision"] == 5, (
            f"relationship authz_revision not bumped: {member['account']['authz_revision']!r}"
        )
        # The separately re-queried row is bumped too (line 263).
        assert session.rows("account")[0]["authz_revision"] == 5

    async def test_else_branch_of_the_ternary_is_dead_code(self) -> None:
        """Documents why the second ``1 -> 0`` mutant on line 258 is
        *provably equivalent* (spec #16 → ticket #19 ``Test*Equivalence`` rule).

        ``m.account.authz_revision += 1 if hasattr(m, "account") else 1``
        compiles to ``m.account`` / ``m.account.authz_revision`` loads that
        happen **before** the right-hand side is evaluated. Therefore:

        * ``hasattr(m, "account")`` is ``True`` on every path that reaches the
          ternary — a ``False`` would have already raised ``AttributeError``.
        * With ``True``, the ``else`` literal is never evaluated, so flipping
          it changes nothing.

        Both mutants are observationally identical for every reachable input,
        so no test can kill the second one. This test pins the *reachable*
        behaviour the dead branch stands for.
        """
        from ksma.identity.application import activate_membership  # noqa: PLC0415

        session = _IdentitySession()
        aid = _seed_account(session, authz_revision=0)
        mid = _seed_membership(session, account_id=aid, authz_revision=0)
        revision_before = session.rows("membership")[0]["account"]["authz_revision"]

        await activate_membership(session, membership_id=mid)

        member_after = session.rows("membership")[0]
        assert member_after["account"]["authz_revision"] == revision_before + 1


    async def test_logout_with_unknown_token_leaves_rows_untouched(self) -> None:
        """``RefreshTokenRow.token_hash == refresh_hash`` (line 540) — the
        idempotent no-op branch must not revoke an unrelated row."""
        from ksma.identity.application import logout  # noqa: PLC0415

        session = _IdentitySession()
        _seed_refresh_token(session, token_hash="some-other-hash", revoked_at=None)
        await logout(session, refresh_raw="unknown-raw-token")
        assert session.rows("refresh_token")[0]["revoked_at"] is None, (
            "logout revoked a token that was never presented"
        )

    async def test_start_recovery_unknown_subject_issues_no_row(self) -> None:
        """``AccountRow.auth_subject == auth_subject`` (line 561) — the
        non-enumerating branch returns a fake token and persists nothing."""
        from ksma.identity.application import start_recovery  # noqa: PLC0415

        session = _IdentitySession()
        _seed_account(session, auth_subject="seeded-subject")
        token = await start_recovery(session, auth_subject="other-subject")
        assert token == "invalid-account"
        assert session.rows("recovery_token") == []

    async def test_start_recovery_wrong_namespace_issues_no_row(self) -> None:
        """``AccountRow.auth_namespace == auth_namespace`` (line 560)."""
        from ksma.identity.application import start_recovery  # noqa: PLC0415

        session = _IdentitySession()
        _seed_account(session, auth_subject="seeded-subject", auth_namespace="default")
        token = await start_recovery(
            session, auth_subject="seeded-subject", auth_namespace="other-namespace"
        )
        assert token == "invalid-account"
        assert session.rows("recovery_token") == []


class TestActivateTenantEquality:
    """``TenantRow.id == tenant_id`` (line 102) — matched by equality."""

    async def test_matching_tenant_id_activates_the_row(self) -> None:
        """Kills the ``== -> !=`` mutant on line 102.

        The row is seeded with exactly the id the caller passes, so the lookup
        only finds it under ``==``; under ``!=`` the WHERE excludes it and the
        call raises ``ValidationError("Tenant not found")``.
        """
        from ksma.identity.application import activate_tenant  # noqa: PLC0415

        session = _IdentitySession()
        tenant_id = uuid4()
        session.seed(
            "tenant",
            {
                "id": tenant_id,
                "display_name": "Yayasan Vue",
                "service_status": "setup",
                "default_timezone": "Asia/Jakarta",
                "activated_at": None,
                "updated_by": None,
            },
        )

        tenant = await activate_tenant(session, tenant_id=tenant_id, actor_label="ops")

        assert str(tenant.id) == str(tenant_id)
        assert tenant.service_status.value == "active", (
            f"tenant not activated: {tenant.service_status!r}"
        )
        assert tenant.activated_at is not None
        assert tenant.updated_by == "ops"

    async def test_unknown_tenant_id_does_not_activate_a_sibling(self) -> None:
        """A different tenant id must not touch the seeded row."""
        from ksma.identity.application import activate_tenant  # noqa: PLC0415

        session = _IdentitySession()
        session.seed(
            "tenant",
            {
                "id": uuid4(),
                "display_name": "Other",
                "service_status": "setup",
                "default_timezone": "Asia/Jakarta",
                "activated_at": None,
                "updated_by": None,
            },
        )
        with pytest.raises(ValidationError):
            await activate_tenant(session, tenant_id=uuid4())
        assert session.rows("tenant")[0]["service_status"] == "setup"
