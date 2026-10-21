"""Unit tests for ``ksma.shared.auth.jwt`` (ADR-0001 §7 + spec #16 ticket #23).

Seam: the public surface of :mod:`ksma.shared.auth.jwt`:
- ``issue_access_token`` — HS256 access JWT carrying the *typed* claim set
  (``sub`` / ``iss`` / ``aud`` / ``iat`` / ``exp`` / ``jti`` plus the
  namespaced ``tz_*`` tenant claims), never the grant set.
- ``decode_access_token`` — verifies signature, expiry, audience, issuer and
  the ``require`` list; maps failures onto ``TokenExpiredError`` /
  ``TokenRevokedError``.
- ``issue_refresh_token`` — opaque 256-bit refresh secret (stored hashed).

Behaviour asserted: the claim *values* (not just round-trip equality), the
401-mapping of every decode failure mode, and the 15-minute TTL. No DB, no
FastAPI app, no network. The secret/issuer/audience come from an in-process
``Settings`` instance, so the env is never consulted.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import patch
from uuid import UUID, uuid4

import jwt as pyjwt
import pytest
from pydantic import SecretStr

from ksma.settings import Settings
from ksma.shared.auth.jwt import (
    decode_access_token,
    issue_access_token,
    issue_refresh_token,
)
from ksma.shared.errors import TokenExpiredError, TokenRevokedError


@pytest.fixture(autouse=True)
def _fixed_settings() -> Any:
    """Pin issuer / audience / secret / TTL so the tests are hermetic."""
    from ksma.settings import get_settings as _real_get_settings  # noqa: PLC0415

    test_settings = Settings(
        env="test",
        log_level="INFO",
        postgres_dsn="postgresql+asyncpg://test:test@localhost:5432/test",
        postgres_dsn_sync="postgresql://test:test@localhost:5432/test",
        jwt_secret=SecretStr("test-secret-do-not-use-in-prod-0123456789"),
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


def _issue(**overrides: Any) -> tuple[str, datetime]:
    kwargs: dict[str, Any] = {
        "subject": uuid4(),
        "memberships": [{"tenant_id": str(uuid4()), "roles": ["AC"]}],
        "current_tenant_id": uuid4(),
        "current_unit_id": uuid4(),
        "authz_revision": 3,
    }
    kwargs.update(overrides)
    return issue_access_token(**kwargs)


def _claims(token: str) -> dict[str, Any]:
    """Decode without verification so the assertions read the raw claim set."""
    return pyjwt.decode(token, options={"verify_signature": False})


class TestIssueAccessTokenClaimSet:
    """Every required claim must be present with the *right* value."""

    def test_claim_set_is_typed_and_complete(self) -> None:
        subject = uuid4()
        tenant_id = uuid4()
        unit_id = uuid4()
        memberships = [{"tenant_id": str(tenant_id), "roles": ["FI", "AC"]}]
        token, expires = _issue(
            subject=subject,
            memberships=memberships,
            current_tenant_id=tenant_id,
            current_unit_id=unit_id,
            authz_revision=7,
        )
        claims = _claims(token)

        assert claims["sub"] == str(subject)
        assert claims["iss"] == "kitapandu-test"
        assert claims["aud"] == "kitapandu-test-web"
        assert claims["tz_memberships"] == memberships
        assert claims["tz_current_tenant"] == str(tenant_id)
        assert claims["tz_current_unit"] == str(unit_id)
        assert claims["tz_authz_revision"] == 7
        # iat / exp are POSIX ints; jti is a 32-hex uuid4 string.
        assert isinstance(claims["iat"], int)
        assert isinstance(claims["exp"], int)
        assert claims["exp"] - claims["iat"] == 900, "access TTL must be 15 minutes"
        # ``issue_access_token`` returns the same instant it encoded (JWT
        # stores whole seconds, so allow sub-second truncation).
        assert abs(
            (expires - datetime.fromtimestamp(claims["exp"], tz=UTC)).total_seconds()
        ) < 1.0
        assert len(claims["jti"]) == 32

    def test_two_tokens_never_share_a_jti(self) -> None:
        """Replay defence: every issuance gets a fresh ``jti``."""
        first, _ = _issue()
        second, _ = _issue()
        assert _claims(first)["jti"] != _claims(second)["jti"]
        assert first != second

    def test_grants_are_never_embedded(self) -> None:
        """ADR-0001 §7: the access token carries identity only."""
        claims = _claims(_issue()[0])
        for forbidden in ("grants", "action_ids", "scope", "roles"):
            assert forbidden not in claims, f"{forbidden!r} must not be a JWT claim"

    def test_null_tenant_and_unit_claims_are_explicit_none(self) -> None:
        """Service-principal style issuance: claims present but null."""
        claims = _claims(_issue(current_tenant_id=None, current_unit_id=None)[0])
        assert "tz_current_tenant" in claims and claims["tz_current_tenant"] is None
        assert "tz_current_unit" in claims and claims["tz_current_unit"] is None

    def test_algorithm_is_hs256(self) -> None:
        token = _issue()[0]
        assert pyjwt.get_unverified_header(token)["alg"] == "HS256"


class TestDecodeAccessToken:
    """Signature / expiry / audience / issuer are all enforced."""

    def test_round_trip_returns_the_same_claims(self) -> None:
        subject = uuid4()
        tenant_id = uuid4()
        token, _ = _issue(subject=subject, current_tenant_id=tenant_id, authz_revision=2)
        claims = decode_access_token(token)
        assert claims["sub"] == str(subject)
        assert claims["tz_current_tenant"] == str(tenant_id)
        assert UUID(claims["sub"]) == subject
        assert claims["tz_authz_revision"] == 2

    def test_expired_token_raises_token_expired(self) -> None:
        """A token whose ``exp`` is in the past maps to ``TokenExpiredError``
        (the 401 ``TOKEN_EXPIRED`` branch of ``current_account_id``)."""
        secret = "test-secret-do-not-use-in-prod-0123456789"
        now = datetime.now(UTC)
        token = pyjwt.encode(
            {
                "sub": str(uuid4()),
                "iss": "kitapandu-test",
                "aud": "kitapandu-test-web",
                "iat": now - timedelta(hours=2),
                "exp": now - timedelta(hours=1),
            },
            secret,
            algorithm="HS256",
        )
        with pytest.raises(TokenExpiredError):
            decode_access_token(token)

    def test_signature_mismatch_raises_token_revoked(self) -> None:
        """Wrong signing key → invalid, never silently accepted."""
        now = datetime.now(UTC)
        forged = pyjwt.encode(
            {
                "sub": str(uuid4()),
                "iss": "kitapandu-test",
                "aud": "kitapandu-test-web",
                "iat": now,
                "exp": now + timedelta(minutes=15),
            },
            "a-different-secret-that-is-long-enough-123456",
            algorithm="HS256",
        )
        with pytest.raises(TokenRevokedError):
            decode_access_token(forged)

    @pytest.mark.parametrize(
        ("claim", "value"),
        [
            pytest.param("aud", "some-other-audience", id="audience_mismatch"),
            pytest.param("iss", "some-other-issuer", id="issuer_mismatch"),
        ],
    )
    def test_wrong_audience_or_issuer_raises(self, claim: str, value: str) -> None:
        now = datetime.now(UTC)
        payload = {
            "sub": str(uuid4()),
            "iss": "kitapandu-test",
            "aud": "kitapandu-test-web",
            "iat": now,
            "exp": now + timedelta(minutes=15),
        }
        payload[claim] = value
        token = pyjwt.encode(
            payload, "test-secret-do-not-use-in-prod-0123456789", algorithm="HS256"
        )
        with pytest.raises(TokenRevokedError):
            decode_access_token(token)

    @pytest.mark.parametrize("missing", ["exp", "iat", "sub", "aud", "iss"])
    def test_incomplete_token_is_rejected(self, missing: str) -> None:
        """The ``require`` list rejects tokens missing any mandatory claim."""
        now = datetime.now(UTC)
        payload: dict[str, Any] = {
            "sub": str(uuid4()),
            "iss": "kitapandu-test",
            "aud": "kitapandu-test-web",
            "iat": now,
            "exp": now + timedelta(minutes=15),
        }
        payload.pop(missing)
        token = pyjwt.encode(
            payload, "test-secret-do-not-use-in-prod-0123456789", algorithm="HS256"
        )
        with pytest.raises(TokenRevokedError):
            decode_access_token(token)

    def test_garbage_token_raises_token_revoked(self) -> None:
        with pytest.raises(TokenRevokedError):
            decode_access_token("not-a-jwt")


class TestIssueRefreshToken:
    """The refresh token is an opaque, high-entropy secret."""

    def test_is_opaque_and_url_safe(self) -> None:
        raw = issue_refresh_token()
        assert isinstance(raw, str)
        assert len(raw) >= 40, f"refresh token too short: {len(raw)}"
        assert set(raw) <= set(
            "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_"
        )
        assert "." not in raw and not raw.count(":")

    def test_is_unique_per_call(self) -> None:
        tokens = {issue_refresh_token() for _ in range(25)}
        assert len(tokens) == 25

    def test_is_not_a_jwt(self) -> None:
        """No claims to read: the DB stores the SHA-256 of this value."""
        raw = issue_refresh_token()
        assert raw.count(".") != 2
        with pytest.raises(TokenRevokedError):
            decode_access_token(raw)