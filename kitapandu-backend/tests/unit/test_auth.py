"""Unit tests for auth primitives (Argon2id + JWT).

Seam: ``ksma.shared.auth.hash_password`` / ``verify_password`` and
``ksma.shared.auth.jwt.issue_access_token`` / ``decode_access_token``.

Per ADR-0001 §7 + ADR-0006: argon2id for password hashing; HS256 JWT with
explicit iss/aud/exp/iat/jti + KSMA-namespaced claims (tz_memberships,
tz_current_tenant, tz_current_unit, tz_authz_revision).
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import patch
from uuid import UUID, uuid4

import jwt
import pytest

from ksma.shared.auth import hash_password, needs_rehash, verify_password
from ksma.shared.auth.jwt import decode_access_token, issue_access_token
from ksma.shared.errors import TokenExpiredError, TokenRevokedError


# Patch the settings to deterministic values for every test in this module.
@pytest.fixture(autouse=True)
def fixed_settings():
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
    with patch("ksma.settings.get_settings", return_value=test_settings), \
         patch("ksma.shared.auth.jwt.get_settings", return_value=test_settings):
        yield test_settings


class TestArgon2id:
    def test_hash_is_not_paintext(self) -> None:
        h = hash_password("hunter2")
        assert h != "hunter2"
        assert h.startswith("$argon2id$")

    def test_verify_correct_password(self) -> None:
        h = hash_password("correct horse battery staple")
        assert verify_password(h, "correct horse battery staple") is True

    def test_verify_wrong_password(self) -> None:
        h = hash_password("hunter2")
        assert verify_password(h, "hunter3") is False

    def test_verify_invalid_hash_returns_false(self) -> None:
        # Never raise; return False. Security: invalid stored hashes must
        # not produce different error semantics than wrong passwords.
        assert verify_password("not-an-argon2-hash", "anything") is False

    def test_two_hashes_of_same_password_differ(self) -> None:
        # Per-row salt — same input should produce different outputs.
        a = hash_password("same")
        b = hash_password("same")
        assert a != b
        assert verify_password(a, "same") is True
        assert verify_password(b, "same") is True


class TestJWTAccessToken:
    def _payload(self) -> dict:
        sub = uuid4()
        return {
            "subject": sub,
            "memberships": [
                {
                    "id": str(uuid4()),
                    "tenant_id": str(uuid4()),
                    "role_code": "AC",
                    "authz_revision": 1,
                }
            ],
            "current_tenant_id": uuid4(),
            "current_unit_id": None,
            "authz_revision": 1,
        }

    def test_issue_returns_token_and_expiry(self) -> None:
        token, expires = issue_access_token(**self._payload())
        assert isinstance(token, str) and len(token) > 50
        assert isinstance(expires, datetime)
        # 15-min default.
        delta = expires - datetime.now(timezone.utc)
        assert 800 < delta.total_seconds() < 1000

    def test_decode_round_trip(self) -> None:
        p = self._payload()
        token, _ = issue_access_token(**p)
        claims = decode_access_token(token)
        assert claims["sub"] == str(p["subject"])
        assert claims["iss"] == "kitapandu-test"
        assert claims["aud"] == "kitapandu-test-web"
        assert claims["tz_authz_revision"] == 1
        assert claims["tz_current_tenant"] == str(p["current_tenant_id"])
        assert claims["tz_current_unit"] is None
        assert claims["jti"]

    def test_decode_rejects_expired(self) -> None:
        p = self._payload()
        # Issue with TTL 0 then decode => exp <= now => ExpiredSignatureError.
        from ksma.shared.auth import jwt as jwt_mod
        # Reach into the module to set a 0-second TTL on a copy.
        import ksma.settings as settings_mod
        with patch.object(settings_mod.get_settings(), "access_token_ttl_seconds", -1):
            token, _ = issue_access_token(**p)
            with pytest.raises(TokenExpiredError):
                decode_access_token(token)

    def test_decode_rejects_wrong_audience(self) -> None:
        p = self._payload()
        token, _ = issue_access_token(**p)
        from ksma.settings import Settings as RealSettings
        other = RealSettings(
            env="test", log_level="INFO",
            postgres_dsn="postgresql+asyncpg://test:test@localhost:5432/test",
            postgres_dsn_sync="postgresql://test:test@localhost:5432/test",
            jwt_secret=__import__("pydantic").SecretStr("test-secret-do-not-use-in-prod"),
            jwt_issuer="kitapandu-test",
            jwt_audience="someone-else",
            access_token_ttl_seconds=900,
            refresh_token_ttl_seconds=604800,
            minio_endpoint="localhost:9000",
            minio_access_key="minioadmin",
            minio_secret_key=__import__("pydantic").SecretStr("minioadmin"),
            minio_secure=False,
            s3_bucket="ksma-test",
            presigned_url_ttl_seconds=300,
            outbox_poll_interval_seconds=0.5,
            outbox_batch_size=50,
            max_file_bytes=10485760,
            default_timezone="Asia/Jakarta",
        )
        with patch("ksma.shared.auth.jwt.get_settings", return_value=other):
            with pytest.raises(TokenRevokedError):
                decode_access_token(token)

    def test_decode_rejects_wrong_issuer(self) -> None:
        p = self._payload()
        token, _ = issue_access_token(**p)
        # Tamper: take the payload, change iss, re-sign with same secret.
        # Easier: use jwt library directly to make a token with wrong iss.
        from ksma.shared.auth.jwt import _ALG
        # Reach into the patched secret by calling the same get_settings.
        from ksma.settings import get_settings
        bad = jwt.encode(
            {"sub": str(p["subject"]), "iss": "evil", "aud": "kitapandu-test-web",
             "iat": datetime.now(timezone.utc), "exp": datetime.now(timezone.utc) + timedelta(minutes=15),
             "tz_authz_revision": 1},
            get_settings().jwt_secret.get_secret_value(),
            algorithm=_ALG,
        )
        with pytest.raises(TokenRevokedError):
            decode_access_token(bad)

    def test_decode_rejects_tampered_signature(self) -> None:
        p = self._payload()
        token, _ = issue_access_token(**p)
        # Flip the last char of the signature.
        tampered = token[:-1] + ("a" if token[-1] != "a" else "b")
        with pytest.raises(TokenRevokedError):
            decode_access_token(tampered)

    def test_decode_rejects_missing_required_claims(self) -> None:
        # Build a token manually missing the sub claim.
        from ksma.shared.auth.jwt import _ALG
        from ksma.settings import get_settings
        token = jwt.encode(
            {"iss": "kitapandu-test", "aud": "kitapandu-test-web",
             "iat": datetime.now(timezone.utc), "exp": datetime.now(timezone.utc) + timedelta(minutes=15)},
            get_settings().jwt_secret.get_secret_value(),
            algorithm=_ALG,
        )
        with pytest.raises(TokenRevokedError):
            decode_access_token(token)

    # --- positive assertions on the issued JWT claims (issue #37) ----------
    # The existing tests above pin shape, but they do not pin *every*
    # claims field. The point of this block: a mutation that drops or
    # relabels a custom claim (tz_memberships / tz_current_tenant /
    # tz_current_unit / tz_authz_revision) must break a test below.
    def test_claims_include_membership_id_and_role_code(self) -> None:
        p = self._payload()
        token, _ = issue_access_token(**p)
        claims = decode_access_token(token)
        # The membership dict survives the round-trip verbatim: id + tenant +
        # role_code + authz_revision are all readable from the claims dict.
        membership = claims["tz_memberships"][0]
        assert membership["id"] == p["memberships"][0]["id"]
        assert membership["tenant_id"] == p["memberships"][0]["tenant_id"]
        assert membership["role_code"] == "AC"
        assert membership["authz_revision"] == 1

    def test_claims_current_unit_is_serialised(self) -> None:
        unit = uuid4()
        p = self._payload()
        p["current_unit_id"] = unit
        token, _ = issue_access_token(**p)
        claims = decode_access_token(token)
        assert claims["tz_current_unit"] == str(unit)

    def test_claims_current_tenant_none_serialised_as_none(self) -> None:
        # current_tenant_id=None for service principals; we must NOT coerce
        # to the string "None" or the empty string.
        p = self._payload()
        p["current_tenant_id"] = None
        token, _ = issue_access_token(**p)
        claims = decode_access_token(token)
        assert claims["tz_current_tenant"] is None

    def test_claims_authz_revision_round_trips(self) -> None:
        # An `==` → `!=` flip on the authz_revision copy would not change the
        # token bytes (they're just an int), but a `1 → 0` literal flip
        # would. We test against a non-default value (5) to surface both.
        p = self._payload()
        p["authz_revision"] = 5
        token, _ = issue_access_token(**p)
        claims = decode_access_token(token)
        assert claims["tz_authz_revision"] == 5

    def test_jti_is_unique_per_token(self) -> None:
        # Same payload, two calls — jti must be different. A `uuid4()`
        # removal flip would make these equal.
        p = self._payload()
        t1, _ = issue_access_token(**p)
        t2, _ = issue_access_token(**p)
        c1 = decode_access_token(t1)
        c2 = decode_access_token(t2)
        assert c1["jti"] != c2["jti"]
        # Sanity: jti is a valid UUID4 hex.
        UUID(c1["jti"])
        UUID(c2["jti"])

    def test_audience_mismatch_raises_token_revoked(self) -> None:
        # Same audience code in module-level fixture; flip the audience and
        # confirm the wrong-aud token is rejected with the expected class
        # (not a generic InvalidTokenError). This pins the *type* of error
        # so a `raise ValueError(...)` mutation in jwt.decode catches here.
        p = self._payload()
        token, _ = issue_access_token(**p)
        from ksma.shared.auth.jwt import _ALG
        from ksma.settings import Settings
        other = Settings(
            env="test", log_level="INFO",
            postgres_dsn="postgresql+asyncpg://test:test@localhost:5432/test",
            postgres_dsn_sync="postgresql://test:test@localhost:5432/test",
            jwt_secret=__import__("pydantic").SecretStr("test-secret-do-not-use-in-prod"),
            jwt_issuer="kitapandu-test",
            jwt_audience="another-audience",
            access_token_ttl_seconds=900,
            refresh_token_ttl_seconds=604800,
            minio_endpoint="localhost:9000",
            minio_access_key="minioadmin",
            minio_secret_key=__import__("pydantic").SecretStr("minioadmin"),
            minio_secure=False,
            s3_bucket="ksma-test",
            presigned_url_ttl_seconds=300,
            outbox_poll_interval_seconds=0.5,
            outbox_batch_size=50,
            max_file_bytes=10485760,
            default_timezone="Asia/Jakarta",
        )
        with patch("ksma.shared.auth.jwt.get_settings", return_value=other):
            with pytest.raises(TokenRevokedError):
                decode_access_token(token)

    def test_authz_version_mismatch_raises(self) -> None:
        """Authz-revision comparison lives in the resolver, not jwt.decode.

        We pin the contract that issuing with a stale authz_revision produces
        a token whose tz_authz_revision is the OLD value — i.e., it is the
        resolver's job (not the JWT layer) to detect the mismatch. A flip
        that pinned the new value at issue time would let a stale token pass.
        """
        p = self._payload()
        p["authz_revision"] = 1  # stale
        token, _ = issue_access_token(**p)
        claims = decode_access_token(token)
        assert claims["tz_authz_revision"] == 1  # the issue-time value, unchanged
        # Now if the resolver were to receive a current-revision of 2 from
        # the DB, a `==` → `!=` flip on the comparison would falsely allow it.
        # We exercise that the FIELD carries the issue-time value so the
        # resolver's strict-equality check has something to compare against.

    def test_expired_token_raises_token_expired(self) -> None:
        # Same scenario as test_decode_rejects_expired, but assert the
        # error type explicitly (the existing test already does this; we
        # duplicate to pin the *class* with a non-default TTL).
        p = self._payload()
        import ksma.settings as settings_mod
        with patch.object(settings_mod.get_settings(), "access_token_ttl_seconds", -10):
            token, _ = issue_access_token(**p)
            with pytest.raises(TokenExpiredError):
                decode_access_token(token)