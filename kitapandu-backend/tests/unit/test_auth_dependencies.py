"""Unit tests for the FastAPI auth dependency chain.

Seam: ``ksma.shared.auth.dependencies``. Public surface:
- ``bearer_token`` — extracts the raw token from ``Authorization: Bearer …``
- ``current_account_id`` — decodes the JWT into claims; 401 on failure
- ``tenant_context_dep`` — resolves a ``TenantContext`` from JWT claims +
  DB rows. Hardened against authz-revision mismatch, missing membership,
  inactive account (per ADR-0001 §7 / ADR-0006 / R01).

Per the parent issue, ``tenant_context_dep`` is in the top-20 CRAP table
(CC 12) and has historically resisted mutation testing because each branch
returns ``HTTPException`` with a specific ``detail.code``. These tests pin
the status codes + machine-readable codes + happy-path return value.

Tests use ``monkeypatch`` to substitute ``session_scope`` and
``set_current_tenant`` with in-process fakes so the dependency can be
exercised without a real Postgres connection.
"""
from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from typing import Any
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import pytest
from fastapi import HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials
from starlette.requests import Request

from ksma.identity.infrastructure.models import AccountRow, MembershipRow
from ksma.shared.auth.dependencies import (
    bearer_token,
    current_account_id,
    tenant_context_dep,
)
from ksma.shared.auth.jwt import (
    decode_access_token,
    issue_access_token,
    issue_refresh_token,
)
from ksma.shared.errors import (
    AuthenticationError,
    TokenExpiredError,
    TokenRevokedError,
)
from ksma.shared.tenancy import ActorKind, TenantContext


# ---------------------------------------------------------------------------
# Settings + fakes -----------------------------------------------------------
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def fixed_settings():
    """Same deterministic Settings as ``tests/unit/test_auth.py``.

    The autouse scope keeps the two test files isolated — ``test_auth.py``
    uses its own copy internally.
    """
    from pydantic import SecretStr

    from ksma.settings import Settings

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
    from unittest.mock import patch

    with patch("ksma.settings.get_settings", return_value=test_settings), \
         patch("ksma.shared.auth.jwt.get_settings", return_value=test_settings):
        yield test_settings


def _make_request() -> Request:
    """Minimal starlette Request — tenant_context_dep writes to ``request.state``."""
    scope: dict[str, Any] = {
        "type": "http",
        "method": "GET",
        "path": "/",
        "headers": [(b"host", b"localhost")],
        "query_string": b"",
        "scheme": "http",
        "server": ("localhost", 80),
        "client": ("localhost", 1234),
    }
    return Request(scope)


def _make_creds(token: str) -> HTTPAuthorizationCredentials:
    return HTTPAuthorizationCredentials(scheme="Bearer", credentials=token)


class _FakeScalarResult:
    """Stand-in for a SQLAlchemy ``Row`` — exposes ``scalar_one_or_none()``."""

    def __init__(self, value: Any) -> None:
        self._value = value

    def scalar_one_or_none(self) -> Any:
        return self._value


class _FakeSession:
    """A tiny AsyncSession stub: dispatches ``execute(...)`` based on a
    pre-loaded sequence of scalars.

    Use with the helper :func:`patch_session_scope` to substitute the
    module's ``session_scope`` async context manager.
    """

    def __init__(self, scalars: list[Any]) -> None:
        self._scalars = list(scalars)
        self.executed: list[Any] = []
        # .connection() is called by ``set_current_tenant`` to issue SET LOCAL.
        # Test code patches set_current_tenant to a no-op, but the connection
        # is sometimes touched in assertions — give it a harmless AsyncMock.
        self.connection = AsyncMock()

    async def execute(self, *args: Any, **kwargs: Any) -> _FakeScalarResult:
        self.executed.append((args, kwargs))
        if not self._scalars:
            return _FakeScalarResult(None)
        # Pop the next pre-loaded scalar in FIFO order. Tests that need to
        # inspect which statement was run can rely on ``self.executed``.
        return _FakeScalarResult(self._scalars.pop(0))


@asynccontextmanager
async def _fake_session_scope(session: _FakeSession):
    yield session


def patch_session_scope(monkeypatch: pytest.MonkeyPatch, session: _FakeSession):
    """Install a fake ``session_scope`` in the dependencies module.

    The dependency imports ``session_scope`` at module load time
    (``from ksma.shared.persistence import session_scope``), so we have to
    patch the *attribute* on the dependencies module, not on the
    persistence module.
    """
    import ksma.shared.auth.dependencies as deps_mod

    @asynccontextmanager
    async def _scope():
        yield session

    monkeypatch.setattr(deps_mod, "session_scope", _scope)


def patch_set_current_tenant(monkeypatch: pytest.MonkeyPatch) -> AsyncMock:
    """Replace ``set_current_tenant`` with a recording no-op.

    The full implementation opens a real connection and issues ``SET LOCAL``;
    for unit tests we don't have a DB. The function is still *called* by
    ``tenant_context_dep`` on the happy path, so we record the call to pin
    that the dependency reaches the GUC-set step (catches an early-return
    mutation).
    """
    import ksma.shared.auth.dependencies as deps_mod

    spy = AsyncMock()
    monkeypatch.setattr(deps_mod, "set_current_tenant", spy)
    return spy


def _build_account(
    *,
    sub: UUID | None = None,
    status: str = "active",
    authz_revision: int = 1,
) -> AccountRow:
    return AccountRow(
        id=sub or uuid4(),
        auth_subject="alice",
        auth_namespace="default",
        display_name="Alice",
        status=status,
        authz_revision=authz_revision,
        password_hash=None,
    )


def _build_membership(
    *,
    account_id: UUID,
    tenant_id: UUID,
    status: str = "active",
) -> MembershipRow:
    return MembershipRow(
        id=uuid4(),
        tenant_id=tenant_id,
        account_id=account_id,
        status=status,
        valid_from=datetime(2024, 1, 1, tzinfo=timezone.utc),
    )


# ---------------------------------------------------------------------------
# bearer_token ---------------------------------------------------------------
# ---------------------------------------------------------------------------


class TestBearerToken:
    """Pin the Authorization-header extraction."""

    @pytest.mark.asyncio
    async def test_missing_credentials_returns_401_authentication_required(self) -> None:
        with pytest.raises(HTTPException) as exc_info:
            await bearer_token(creds=None)
        assert exc_info.value.status_code == status.HTTP_401_UNAUTHORIZED
        assert exc_info.value.detail == {"code": "AUTHENTICATION_REQUIRED"}

    @pytest.mark.asyncio
    async def test_empty_credentials_returns_401(self) -> None:
        """An empty Bearer token is treated identically to a missing one —
        the FastAPI security helper does not call us when the scheme is
        missing, but a present-but-empty credentials string must still 401."""
        with pytest.raises(HTTPException) as exc_info:
            await bearer_token(creds=_make_creds(""))
        assert exc_info.value.status_code == status.HTTP_401_UNAUTHORIZED
        assert exc_info.value.detail == {"code": "AUTHENTICATION_REQUIRED"}

    @pytest.mark.asyncio
    async def test_valid_credentials_returns_raw_token(self) -> None:
        token = issue_refresh_token()  # opaque, not a JWT
        out = await bearer_token(creds=_make_creds(token))
        assert out == token
        assert isinstance(out, str)


# ---------------------------------------------------------------------------
# current_account_id ---------------------------------------------------------
# ---------------------------------------------------------------------------


class TestCurrentAccountId:
    """Pin the JWT-decode wrapper that maps PyJWT exceptions to 401 codes."""

    @pytest.mark.asyncio
    async def test_invalid_signature_returns_401_token_revoked(self) -> None:
        """A tampered signature must map to ``TOKEN_REVOKED`` (401)."""
        token, _ = issue_access_token(
            subject=uuid4(),
            memberships=[{"tenant_id": str(uuid4()), "role_code": "AC", "authz_revision": 1}],
            current_tenant_id=uuid4(),
            current_unit_id=None,
            authz_revision=1,
        )
        tampered = token[:-1] + ("a" if token[-1] != "a" else "b")
        with pytest.raises(HTTPException) as exc_info:
            await current_account_id(token=tampered)
        assert exc_info.value.status_code == status.HTTP_401_UNAUTHORIZED
        assert exc_info.value.detail == {"code": "TOKEN_REVOKED"}

    @pytest.mark.asyncio
    async def test_expired_token_returns_401_token_expired(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An expired token must map to ``TOKEN_EXPIRED`` (401), NOT
        ``TOKEN_REVOKED``. Catches an ``==`` mutation that confuses the two
        error codes."""
        sub = uuid4()
        tenant_id = uuid4()
        # Issue with TTL=-1 → exp == iat - 1s → immediately expired.
        # Pin a fresh TTL on the wrapped settings object the autouse fixture
        # already provides.
        from ksma.shared.auth import jwt as jwt_mod

        # Fetch the live settings via the wrapped accessor.
        live_settings = jwt_mod.get_settings()
        monkeypatch.setattr(live_settings, "access_token_ttl_seconds", -1)

        token, _ = issue_access_token(
            subject=sub,
            memberships=[{"tenant_id": str(tenant_id), "role_code": "AC", "authz_revision": 1}],
            current_tenant_id=tenant_id,
            current_unit_id=None,
            authz_revision=1,
        )
        with pytest.raises(HTTPException) as exc_info:
            await current_account_id(token=token)
        assert exc_info.value.status_code == status.HTTP_401_UNAUTHORIZED
        assert exc_info.value.detail == {"code": "TOKEN_EXPIRED"}

    @pytest.mark.asyncio
    async def test_valid_token_returns_claims_dict(self) -> None:
        """Happy path: a fresh token decodes to the expected claim shape."""
        sub = uuid4()
        tenant_id = uuid4()
        token, _ = issue_access_token(
            subject=sub,
            memberships=[{"tenant_id": str(tenant_id), "role_code": "AC", "authz_revision": 1}],
            current_tenant_id=tenant_id,
            current_unit_id=None,
            authz_revision=1,
        )
        claims = await current_account_id(token=token)
        assert claims["sub"] == str(sub)
        assert claims["iss"] == "kitapandu-test"
        assert claims["aud"] == "kitapandu-test-web"
        assert claims["tz_current_tenant"] == str(tenant_id)


# ---------------------------------------------------------------------------
# tenant_context_dep — claim-validation early-return paths
# (no DB session required; reach in before ``async with session_scope()``)
# ---------------------------------------------------------------------------


class TestTenantContextClaimValidation:
    """The dependency rejects malformed claims BEFORE opening a DB session.

    These tests pin the early-return branches: a missing ``sub`` /
    ``tz_current_tenant`` / ``tz_authz_revision``, or a non-UUID
    ``tz_current_tenant``, must produce ``401 INVALID_CLAIMS``.
    """

    @pytest.mark.asyncio
    async def test_missing_sub_returns_401_invalid_claims(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        ctx = await _invoke_tenant_context(
            monkeypatch,
            session=_FakeSession([]),
            claims={"tz_current_tenant": str(uuid4()), "tz_authz_revision": 1},
        )
        assert isinstance(ctx, HTTPException)
        assert ctx.status_code == status.HTTP_401_UNAUTHORIZED
        assert ctx.detail == {"code": "INVALID_CLAIMS"}

    @pytest.mark.asyncio
    async def test_missing_tenant_returns_401_invalid_claims(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        ctx = await _invoke_tenant_context(
            monkeypatch,
            session=_FakeSession([]),
            claims={"sub": str(uuid4()), "tz_authz_revision": 1},
        )
        assert isinstance(ctx, HTTPException)
        assert ctx.status_code == status.HTTP_401_UNAUTHORIZED
        assert ctx.detail == {"code": "INVALID_CLAIMS"}

    @pytest.mark.asyncio
    async def test_missing_authz_revision_returns_401_invalid_claims(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        ctx = await _invoke_tenant_context(
            monkeypatch,
            session=_FakeSession([]),
            claims={"sub": str(uuid4()), "tz_current_tenant": str(uuid4())},
        )
        assert isinstance(ctx, HTTPException)
        assert ctx.status_code == status.HTTP_401_UNAUTHORIZED
        assert ctx.detail == {"code": "INVALID_CLAIMS"}

    @pytest.mark.asyncio
    async def test_none_tenant_returns_401_invalid_claims(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``tz_current_tenant=None`` is treated as "missing" — falsey.

        Pins the ``not tenant_id_raw`` check (a flip to ``is None`` would
        accept empty strings)."""
        ctx = await _invoke_tenant_context(
            monkeypatch,
            session=_FakeSession([]),
            claims={
                "sub": str(uuid4()),
                "tz_current_tenant": None,
                "tz_authz_revision": 1,
            },
        )
        assert isinstance(ctx, HTTPException)
        assert ctx.status_code == status.HTTP_401_UNAUTHORIZED
        assert ctx.detail == {"code": "INVALID_CLAIMS"}

    @pytest.mark.asyncio
    async def test_malformed_tenant_uuid_returns_401_invalid_claims(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A non-UUID ``tz_current_tenant`` must be caught and 401'd — the
        dependency must NOT let a bad UUID reach ``set_current_tenant``
        (where it would surface as a 500).

        Regression guard for #53: ``tenant_context_dep`` once called
        ``UUID(...)`` without importing the symbol, so this branch raised
        ``NameError`` instead of 401'ing. It now asserts against the real
        production module with no ``UUID`` monkeypatch workaround."""
        ctx = await _invoke_tenant_context(
            monkeypatch,
            session=_FakeSession([]),
            claims={
                "sub": str(uuid4()),
                "tz_current_tenant": "not-a-uuid",
                "tz_authz_revision": 1,
            },
        )
        assert isinstance(ctx, HTTPException)
        assert ctx.status_code == status.HTTP_401_UNAUTHORIZED
        assert ctx.detail == {"code": "INVALID_CLAIMS"}


# ---------------------------------------------------------------------------
# tenant_context_dep — DB-load branches (fake session_scope)
# ---------------------------------------------------------------------------


class TestTenantContextDBValidation:
    """The DB-load branches.

    Each test installs a fake ``session_scope`` that returns pre-loaded
    ``AccountRow`` + ``MembershipRow`` instances, then asserts the exact
    4xx code + status returned by the dependency.
    """

    @pytest.mark.asyncio
    async def test_account_not_found_returns_401_invalid_credentials(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        session = _FakeSession([None])  # AccountRow lookup returns None
        ctx = await _invoke_tenant_context(
            monkeypatch,
            session=session,
            claims=_valid_claims(),
        )
        assert isinstance(ctx, HTTPException)
        assert ctx.status_code == status.HTTP_401_UNAUTHORIZED
        assert ctx.detail == {"code": "INVALID_CREDENTIALS"}
        # Should NOT have queried for the membership (early bail).
        assert len(session.executed) == 1

    @pytest.mark.asyncio
    async def test_account_inactive_returns_401_invalid_credentials(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        session = _FakeSession(
            [_build_account(status="disabled")]
        )
        ctx = await _invoke_tenant_context(
            monkeypatch,
            session=session,
            claims=_valid_claims(),
        )
        assert isinstance(ctx, HTTPException)
        assert ctx.status_code == status.HTTP_401_UNAUTHORIZED
        assert ctx.detail == {"code": "INVALID_CREDENTIALS"}

    @pytest.mark.asyncio
    async def test_authz_revision_mismatch_returns_401_authz_revision_mismatch(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The access token's ``tz_authz_revision`` is stale (token says 1,
        account row says 7) → the dependency MUST raise 401 with code
        ``AUTHZ_REVISION_MISMATCH`` (R01)."""
        session = _FakeSession(
            [_build_account(authz_revision=7)]  # token says 1
        )
        ctx = await _invoke_tenant_context(
            monkeypatch,
            session=session,
            claims=_valid_claims(authz_revision=1),
        )
        assert isinstance(ctx, HTTPException)
        assert ctx.status_code == status.HTTP_401_UNAUTHORIZED
        assert ctx.detail == {"code": "AUTHZ_REVISION_MISMATCH"}

    @pytest.mark.asyncio
    async def test_membership_not_found_returns_403_membership_inactive(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Authn-passed; authz-fails because there is no membership for the
        claimed tenant. Per ADR-0001 §3 the response is 403
        ``MEMBERSHIP_INACTIVE`` (NOT 401) — 401 is for missing/invalid
        credentials; 403 is for valid-identity-but-no-rights."""
        account_id = uuid4()
        tenant_id = uuid4()
        session = _FakeSession([
            _build_account(sub=account_id, authz_revision=1),
            None,  # membership lookup returns None
        ])
        ctx = await _invoke_tenant_context(
            monkeypatch,
            session=session,
            claims=_valid_claims(account_id=account_id, tenant_id=tenant_id),
        )
        assert isinstance(ctx, HTTPException)
        assert ctx.status_code == status.HTTP_403_FORBIDDEN
        assert ctx.detail == {"code": "MEMBERSHIP_INACTIVE"}

    @pytest.mark.asyncio
    async def test_membership_inactive_returns_403_membership_inactive(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Membership status is not 'active' (e.g. suspended/revoked) → 403."""
        account_id = uuid4()
        tenant_id = uuid4()
        session = _FakeSession([
            _build_account(sub=account_id, authz_revision=1),
            _build_membership(
                account_id=account_id,
                tenant_id=tenant_id,
                status="suspended",
            ),
        ])
        ctx = await _invoke_tenant_context(
            monkeypatch,
            session=session,
            claims=_valid_claims(account_id=account_id, tenant_id=tenant_id),
        )
        assert isinstance(ctx, HTTPException)
        assert ctx.status_code == status.HTTP_403_FORBIDDEN
        assert ctx.detail == {"code": "MEMBERSHIP_INACTIVE"}

    @pytest.mark.asyncio
    async def test_happy_path_returns_tenant_context(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """All checks pass → dependency returns a fully-populated
        ``TenantContext`` AND stashes it on ``request.state`` AND calls
        ``set_current_tenant`` to set the per-tx GUC."""
        account_id = uuid4()
        tenant_id = uuid4()
        unit_id = uuid4()
        membership_id = uuid4()  # popped to compare with the resolved ctx

        # Build the membership with a known ID, then make _FakeSession return it.
        membership = MembershipRow(
            id=membership_id,
            tenant_id=tenant_id,
            account_id=account_id,
            status="active",
            valid_from=datetime(2024, 1, 1, tzinfo=timezone.utc),
        )
        session = _FakeSession([
            _build_account(sub=account_id, authz_revision=1),
            membership,
        ])
        patch_session_scope(monkeypatch, session)
        spy = patch_set_current_tenant(monkeypatch)

        request = _make_request()
        ctx = await tenant_context_dep(
            request,
            claims=_valid_claims(
                account_id=account_id,
                tenant_id=tenant_id,
                unit_id=unit_id,
                authz_revision=1,
                memberships=[{"tenant_id": str(tenant_id), "roles": ["AC"]}],
            ),
        )
        # Return type contract.
        assert isinstance(ctx, TenantContext)
        # `claims["sub"]` was a string and that string is what the dependency
        # passes to TenantContext.account_id. The contract pins the string
        # round-trip; the dataclass then re-parses via UUID() in
        # constructors upstream. We assert the raw equality first.
        assert str(ctx.account_id) == str(account_id)
        assert ctx.tenant_id == tenant_id
        # ``tz_current_unit`` round-trips as a string (the dependency does
        # not re-parse it). Pins the assignment ``unit_id = claims.get(...)``.
        assert ctx.unit_id == str(unit_id)
        assert ctx.membership_id == membership_id
        assert ctx.actor_kind is ActorKind.ACCOUNT
        assert ctx.authz_revision == 1
        assert ctx.role_codes == ("AC",)

        # Stashed on request.state for downstream handlers.
        assert request.state.tenant_context is ctx

        # The tenant GUC was set on the session.
        assert spy.await_count == 1
        # And the call carried the resolved tenant_id.
        spy.assert_awaited_once()
        args, _kwargs = spy.call_args
        # The signature is ``set_current_tenant(session, tenant_id)``
        # so the second positional arg is the resolved tenant.
        assert args[1] == tenant_id

    @pytest.mark.asyncio
    async def test_happy_path_role_codes_default_empty_when_no_roles(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """If the JWT's ``tz_memberships`` carries no ``roles`` key, the
        resolved ``TenantContext.role_codes`` must be an empty tuple — the
        dependency must not error out on missing fields."""
        account_id = uuid4()
        tenant_id = uuid4()
        membership = _build_membership(
            account_id=account_id, tenant_id=tenant_id,
        )
        session = _FakeSession([
            _build_account(sub=account_id, authz_revision=1),
            membership,
        ])
        patch_session_scope(monkeypatch, session)
        patch_set_current_tenant(monkeypatch)

        ctx = await tenant_context_dep(
            _make_request(),
            claims=_valid_claims(
                account_id=account_id,
                tenant_id=tenant_id,
                memberships=[{"tenant_id": str(tenant_id)}],  # no "roles"
            ),
        )
        assert ctx.role_codes == ()


# ---------------------------------------------------------------------------
# Refresh-token rotation: replay rejects (R02 / parent issue #3 invariant)
# ---------------------------------------------------------------------------


class TestRefreshTokenRotationReplay:
    """Pin the R02 contract: replaying a token that has already been rotated
    must be rejected (the underlying auth claim is no longer valid).

    The full rotation flow lives in ``ksma.identity.application.refresh``
    (it needs an ``AsyncSession``). We exercise it via a fake session
    that simulates the post-rotation state. This pins the seam: a constant
    ``return None`` mutation in ``refresh()`` would fail this test because
    the *replay* code path is the one being verified, and any shortcut
    (e.g. accepting a revoked row, or skipping the ``revoked_at`` check)
    breaks the assertion that the second call raises.
    """

    @pytest.mark.asyncio
    async def test_rotated_token_replay_raises_authentication_error(self) -> None:
        """The seam: ``ksma.identity.application.refresh`` must reject a
        replayed refresh token by raising ``AuthenticationError("Refresh
        token revoked")`` once the row's ``revoked_at`` has been set by a
        prior rotation.

        ``refresh()`` takes an ``AsyncSession`` directly; the rotation flow
        doesn't open a fresh ``session_scope()``, so passing the fake
        session as the argument is sufficient.
        """
        from ksma.identity.application import _hash_refresh, refresh
        from ksma.identity.infrastructure.models import RefreshTokenRow

        raw = issue_refresh_token()
        rotated_row = RefreshTokenRow(
            id=uuid4(),
            tenant_id=uuid4(),
            account_id=uuid4(),
            membership_id=uuid4(),
            token_hash=_hash_refresh(raw),
            issued_at=datetime(2024, 1, 1, tzinfo=timezone.utc),
            expires_at=datetime(2030, 1, 1, tzinfo=timezone.utc),
            revoked_at=datetime(2024, 6, 1, tzinfo=timezone.utc),
            revoked_reason="rotated",
        )
        # The early-rejection branch only queries the refresh-token row by
        # hash; one scalar is enough. Pins the ``rt.revoked_at is not None``
        # branch — a `==` operator flip would let the rotation proceed.
        session = _FakeSession([rotated_row])
        with pytest.raises(AuthenticationError) as exc_info:
            await refresh(session, refresh_raw=raw)  # type: ignore[arg-type]
        # The error is the rotation-rejection form, not the "row not found"
        # form. Pins the text of the message because the token type depends
        # on it being clear that the replay path is the violation.
        assert "revoked" in str(exc_info.value).lower()
        # Exactly one query was issued (the refresh-token lookup); the
        # AccountRow + MembershipRow lookups must NOT run because the
        # rejection is short-circuited.
        assert len(session.executed) == 1

    @pytest.mark.asyncio
    async def test_rotated_token_replay_raises_4xx_mappable_error(self) -> None:
        """``AuthenticationError`` is the 4xx-mappable class — confirms it
        has the right ``http_status`` and ``code`` per ``ksma.shared.errors``
        so the global FastAPI handler converts it to a 401.

        Pins the hierarchy: ``refresh()`` must raise a class whose
        ``http_status`` is set in the 4xx range (specifically the authen
        branch at 401) so the global exception handler in ``ksma.main``
        can map it to a real HTTP response."""
        from ksma.identity.application import _hash_refresh, refresh
        from ksma.identity.infrastructure.models import RefreshTokenRow

        raw = issue_refresh_token()
        rotated_row = RefreshTokenRow(
            id=uuid4(),
            tenant_id=uuid4(),
            account_id=uuid4(),
            membership_id=uuid4(),
            token_hash=_hash_refresh(raw),
            issued_at=datetime(2024, 1, 1, tzinfo=timezone.utc),
            expires_at=datetime(2030, 1, 1, tzinfo=timezone.utc),
            revoked_at=datetime(2024, 6, 1, tzinfo=timezone.utc),
            revoked_reason="rotated",
        )
        session = _FakeSession([rotated_row])
        with pytest.raises(AuthenticationError) as exc_info:
            await refresh(session, refresh_raw=raw)  # type: ignore[arg-type]
        # The error maps to 4xx — pins the authen-class hierarchy.
        assert exc_info.value.http_status >= 400
        assert exc_info.value.http_status < 500
        # Inherits the right code from the AuthenticationError class so
        # the global handler returns a 401 INVALID_CREDENTIALS-style
        # envelope.
        assert exc_info.value.http_status == 401


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _valid_claims(
    *,
    account_id: UUID | None = None,
    tenant_id: UUID | None = None,
    unit_id: UUID | None = None,
    authz_revision: int = 1,
    memberships: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    tid = tenant_id or uuid4()
    if memberships is None:
        memberships = [{"tenant_id": str(tid), "role_code": "AC", "authz_revision": 1}]
    return {
        "sub": str(account_id or uuid4()),
        "tz_current_tenant": str(tid),
        "tz_current_unit": str(unit_id) if unit_id else None,
        "tz_authz_revision": authz_revision,
        "tz_memberships": memberships,
    }


async def _invoke_tenant_context(
    monkeypatch: pytest.MonkeyPatch,
    *,
    session: _FakeSession,
    claims: dict[str, Any],
):
    """Invoke ``tenant_context_dep`` and return the result OR the raised
    ``HTTPException``. Helper for tests that need to assert the exact
    exception detail without wrapping the assertion in ``with pytest.raises``
    (which clashes with the early-return style)."""
    patch_session_scope(monkeypatch, session)
    patch_set_current_tenant(monkeypatch)

    try:
        return await tenant_context_dep(_make_request(), claims=claims)
    except HTTPException as exc:
        return exc
