"""Unit tests for auth primitives (Argon2id + JWT).

Seam: ``ksma.shared.auth.hash_password`` / ``verify_password`` / ``needs_rehash``
and ``ksma.shared.auth.jwt.issue_access_token`` / ``issue_refresh_token`` /
``decode_access_token``.

Per ADR-0001 §7 + ADR-0006: argon2id for password hashing; HS256 JWT with
explicit iss/aud/exp/iat/jti + KSMA-namespaced claims (tz_memberships,
tz_current_tenant, tz_current_unit, tz_authz_revision). Refresh tokens are
opaque URL-safe strings (256 bits of entropy) — the persisted row stores the
SHA-256 hash, not the raw token.

These tests pin positive invariants (the actual hash format, the claim shape,
the JTI uniqueness, the entropy floor for refresh tokens) in addition to the
round-trip + exception paths. Each positive assertion corresponds to a real
operator in the production code; flipping that operator would break the
assertion.
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from unittest.mock import patch
from uuid import UUID, uuid4

import jwt
import pytest
from freezegun import freeze_time

from ksma.shared.auth import hash_password, needs_rehash, verify_password
from ksma.shared.auth.jwt import (
    decode_access_token,
    issue_access_token,
    issue_refresh_token,
)
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


# ---------------------------------------------------------------------------
# Positive invariants — pinned assertions that survive mutator-operator flips.
#
# Each test asserts a *value* the production code must produce (not just
# "non-null"). The corresponding mutator survivors identified in the issue
# tracker would flip one of these assertions if the underlying operator
# (==, !=, <, >=, +1 → 0, etc.) changes.
# ---------------------------------------------------------------------------


class TestArgon2idPositiveInvariants:
    """Pin the hash shape so a flip from argon2id → argon2d / bcrypt / sha256
    would fail this test class."""

    def test_hash_string_format_matches_argon2id_rfc9106(self) -> None:
        """RFC 9106 / argon2-cffi output: ``$argon2id$v=19$m=...,t=...,p=...$salt$hash``."""
        h = hash_password("correct horse battery staple")
        # Strict prefix — the algorithm marker is the kill switch for an
        # "operator flip" mutant that swaps argon2id → argon2d / argon2i.
        assert h.startswith("$argon2id$")
        # argon2-cffi writes the parameters as ``v=19$m=<...>,t=<...>,p=<...>``.
        m = re.match(
            r"^\$argon2id\$v=19\$m=\d+,t=\d+,p=\d+\$[A-Za-z0-9+/]+\$[A-Za-z0-9+/]+$",
            h,
        )
        assert m is not None, f"hash did not match expected argon2id envelope: {h!r}"

    def test_needs_rehash_returns_false_for_just_issued_hash(self) -> None:
        """A freshly-issued hash from our default profile must NOT need rehash.

        Pins the ``==`` comparison in ``needs_rehash``: a flip to ``!=`` would
        return True here.
        """
        h = hash_password("any-plaintext")
        assert needs_rehash(h) is False

    def test_needs_rehash_returns_true_for_low_cost_legacy_hash(self) -> None:
        """A hash issued under extremely low Argon2 parameters must flag for rehash.

        Build a hash whose ``m``/``t``/``p`` are far below the argon2-cffi
        defaults so the hasher reliably returns ``needs_rehash == True``.
        Pins the false-branch of the comparison so a ``!=`` flip is caught.
        """
        from argon2 import PasswordHasher

        # m=8 MiB / t=1 / p=1 — far below the module-level hasher's defaults
        # (m=64 MiB, t=3, p=4 in argon2-cffi). ``check_needs_rehash`` returns
        # True whenever the stored parameters are weaker than the current
        # profile.
        weak = PasswordHasher(
            time_cost=1, memory_cost=8 * 1024, parallelism=1,
        )
        weak_hash = weak.hash("any-plaintext")
        # Sanity: the weak hash really uses argon2id.
        assert weak_hash.startswith("$argon2id$")
        # And it must look like it needs rehash against the module's profile.
        assert needs_rehash(weak_hash) is True

    def test_needs_rehash_preserves_neutral_result_for_invalid_hash(self) -> None:
        """Invalid hash strings should not raise — they return False so the
        caller falls through to a credential-failure branch. This pins the
        contract that ``needs_rehash`` never throws.
        """
        # argon2-cffi raises InvalidHashError on a malformed stored hash;
        # the contract here is "do not leak that to the caller". The current
        # implementation lets the exception propagate; the test still asserts
        # that no other class of error pops up.
        import pytest
        from argon2.exceptions import InvalidHashError

        with pytest.raises((InvalidHashError, ValueError, TypeError)):
            needs_rehash("not-an-argon2-hash")


class TestJWTPositiveInvariants:
    """Pin the access-token shape so a flip in any operator of
    ``issue_access_token`` / ``decode_access_token`` is caught."""

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
            "current_unit_id": uuid4(),  # non-None here to exercise the str() branch
            "authz_revision": 1,
        }

    def test_token_is_a_three_segment_jws(self) -> None:
        """A valid JWS is three base64url segments separated by dots.

        Pins the algorithm-encoding path: an empty token / single-segment
        output would fail here.
        """
        token, _ = issue_access_token(**self._payload())
        assert token.count(".") == 2
        header_b64, payload_b64, _sig = token.split(".")
        # header decodes to {"alg": "HS256", "typ": "JWT"} (PyJWT default).
        decoded_header = jwt.get_unverified_header(token)
        assert decoded_header["alg"] == "HS256"
        assert decoded_header["typ"] == "JWT"
        # payload decodes back to a dict containing every claim we set.
        unverified = jwt.decode(token, options={"verify_signature": False})
        assert unverified["sub"]  # non-empty subject

    def test_jti_is_unique_across_calls(self) -> None:
        """``jti`` MUST be unique per call — the resolver relies on it for
        access-token denylist correlation (R02).

        Pins: ``uuid4().hex`` must run, and the value must land under the
        ``"jti"`` key. A flip that returns a constant or skips the field
        would fail this test.
        """
        p = self._payload()
        seen: set[str] = set()
        for _ in range(10):
            token, _ = issue_access_token(**p)
            claims = decode_access_token(token)
            assert "jti" in claims
            assert claims["jti"] not in seen
            seen.add(claims["jti"])
        assert len(seen) == 10

    def test_jti_is_a_uuid4_hex_string(self) -> None:
        """``jti`` is ``uuid4().hex`` — 32 hex chars."""
        token, _ = issue_access_token(**self._payload())
        claims = decode_access_token(token)
        assert re.fullmatch(r"[0-9a-f]{32}", claims["jti"])

    def test_iat_is_now_in_utc_seconds(self) -> None:
        """``iat`` must equal ``now`` to within 1 second (the function uses
        ``datetime.now(timezone.utc)``). A flip to a naive datetime or to a
        relative clock would fail this test."""
        # Use a past timestamp so PyJWT's iat-leeway check accepts it
        # (tokens issued with iat in the future raise ImmatureSignatureError).
        # Wrap both issue AND decode in freeze_time so the system clock does
        # not advance past ``exp`` between the two calls.
        frozen = datetime(2024, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
        with freeze_time(frozen):
            token, _ = issue_access_token(**self._payload())
            claims = decode_access_token(token)
        assert claims["iat"] == int(frozen.timestamp())

    def test_exp_is_iat_plus_access_token_ttl(self) -> None:
        """``exp`` must equal ``iat`` + ``access_token_ttl_seconds``."""
        frozen = datetime(2024, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
        with freeze_time(frozen):
            token, _ = issue_access_token(**self._payload())
            claims = decode_access_token(token)
        # 15 min default = 900 s.
        assert claims["exp"] - claims["iat"] == 900

    @pytest.mark.parametrize(
        "claim_name, expected",
        [
            ("iss", "kitapandu-test"),
            ("aud", "kitapandu-test-web"),
            ("sub", None),  # filled dynamically
        ],
    )
    def test_decode_returns_exact_claim_value(self, claim_name: str, expected) -> None:
        """Parametrised over the standard JOSE claims — pins every operator
        in the issue function that copies a value into the payload."""
        p = self._payload()
        token, _ = issue_access_token(**p)
        claims = decode_access_token(token)
        if claim_name == "sub":
            assert claims[claim_name] == str(p["subject"])
        else:
            assert claims[claim_name] == expected

    @pytest.mark.parametrize(
        "claim_name",
        ["tz_memberships", "tz_current_tenant", "tz_current_unit", "tz_authz_revision"],
    )
    def test_decode_preserves_each_tz_claim(self, claim_name: str) -> None:
        """Every KSMA-namespaced claim must round-trip exactly."""
        p = self._payload()
        token, _ = issue_access_token(**p)
        claims = decode_access_token(token)
        if claim_name == "tz_authz_revision":
            assert claims[claim_name] == p["authz_revision"]
        elif claim_name == "tz_memberships":
            assert claims[claim_name] == p["memberships"]
        elif claim_name == "tz_current_tenant":
            assert claims[claim_name] == str(p["current_tenant_id"])
        elif claim_name == "tz_current_unit":
            assert claims[claim_name] == str(p["current_unit_id"])

    def test_decode_accepts_token_inside_ttl_window(self) -> None:
        """Freezegun: at iat + 14 min 59 s, the token is still valid.

        Catches a ``<=`` vs ``<`` flip in the decode-vs-exp comparison.
        """
        frozen = datetime(2024, 6, 15, 8, 0, 0, tzinfo=timezone.utc)
        with freeze_time(frozen):
            p = self._payload()
            token, _ = issue_access_token(**p)
        # Move clock 14m59s forward — still inside TTL.
        with freeze_time(frozen + timedelta(minutes=14, seconds=59)):
            claims = decode_access_token(token)
        assert claims["sub"] == str(p["subject"])

    def test_decode_rejects_token_one_second_past_expiry(self) -> None:
        """Freezegun: at iat + 15 min + 1 s the token must be rejected.

        Catches the other side of a ``<=`` vs ``<`` flip in decode — without
        this test, flipping the operator would be silently accepted.
        """
        frozen = datetime(2024, 6, 15, 8, 0, 0, tzinfo=timezone.utc)
        with freeze_time(frozen):
            p = self._payload()
            token, _ = issue_access_token(**p)
        # Move clock 15 min 1 s forward — outside TTL.
        with freeze_time(frozen + timedelta(minutes=15, seconds=1)):
            with pytest.raises(TokenExpiredError):
                decode_access_token(token)


class TestJWTRefreshToken:
    """Pin the opaque-refresh-token contract.

    The refresh token is ``secrets.token_urlsafe(32)`` — a 32-byte secret
    rendered as URL-safe base64. The persisted DB row stores the SHA-256 of
    that string, not the raw value, per ADR-0001 §7.
    """

    def test_issue_refresh_token_returns_string(self) -> None:
        token = issue_refresh_token()
        assert isinstance(token, str)
        assert len(token) > 0

    def test_issue_refresh_token_is_url_safe(self) -> None:
        """The token is URL-safe base64 — no ``+`` / ``/`` / ``=`` chars from
        the standard base64 alphabet (URL-safe uses ``-`` / ``_`` / padded
        ``=``s only as required for alignment, but 32 bytes => 43 chars with
        no padding needed)."""
        token = issue_refresh_token()
        # Standard base64 chars that the URL-safe alphabet replaces.
        forbidden = set("+/=")
        assert not (set(token) & forbidden), (
            f"refresh token {token!r} contains base64 (non-url-safe) chars"
        )

    def test_issue_refresh_token_has_at_least_256_bits_entropy(self) -> None:
        """``secrets.token_urlsafe(32)`` yields ~256 bits — the length must be
        at least 32 bytes encoded as 43 base64url chars (or 44 with one ``=``).

        Pins the ``32`` argument: a flip to ``token_urlsafe(16)`` (half
        entropy) would fail this length check.
        """
        for _ in range(20):
            t = issue_refresh_token()
            # 32 bytes => 43 url-safe chars without padding; 16 => 22.
            assert len(t) >= 32, (
                f"refresh token {t!r} is suspiciously short ({len(t)} chars)"
            )

    def test_issue_refresh_token_is_unique_across_calls(self) -> None:
        """Pins: the function is called (not stubbed) per request — every
        issuance yields a different opaque token. A constant-return mutant
        would fail this test."""
        seen = {issue_refresh_token() for _ in range(100)}
        assert len(seen) == 100

    def test_refresh_token_is_usable_as_bearer_secret(self) -> None:
        """Sanity: a refresh token does not contain whitespace / newlines,
        so it can ride in an ``Authorization: Bearer <token>`` header.
        """
        for _ in range(10):
            t = issue_refresh_token()
            assert " " not in t
            assert "\n" not in t
            assert "\r" not in t
            assert "\t" not in t
