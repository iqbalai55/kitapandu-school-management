"""Unit tests for the ADR-0007 admin-token gate.

These cover the pure policy layer (``ksma.shared.auth.admin_token``): dev mode
(unconfigured => open), and the constant-time Bearer compare when a token is
configured. The HTTP layer is covered by tests/integration/test_admin_token.py.
"""
from __future__ import annotations

import logging

import pytest

from ksma.shared.auth.admin_token import (
    ADMIN_PATH_PREFIX,
    AdminTokenPolicy,
    is_authorized,
    load_policy,
)

CONFIGURED = AdminTokenPolicy(token="s3cr3t-admin-token", is_configured=True)
UNCONFIGURED = AdminTokenPolicy(token=None, is_configured=False)


# --- dev / no-token mode -----------------------------------------------------


def test_is_authorized_unconfigured_returns_true() -> None:
    """Dev mode: no token configured => every header (even wrong) passes."""
    assert is_authorized(UNCONFIGURED, None) is True
    assert is_authorized(UNCONFIGURED, "") is True
    assert is_authorized(UNCONFIGURED, "Bearer totally-wrong") is True
    assert is_authorized(UNCONFIGURED, "Token whatever") is True


def test_load_policy_unconfigured_warns_once(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.WARNING, logger="ksma.shared.auth.admin_token"):
        policy = load_policy(None)
        policy2 = load_policy("")
    assert policy.is_configured is False
    assert policy.token is None
    assert policy2.is_configured is False
    # load_policy is called once at startup, so two calls => two warning lines
    # (the caller is responsible for calling it once).
    assert caplog.text.count("KITAPANDU_ADMIN_TOKEN is unset") == 2
    assert "OPEN" in caplog.text


def test_load_policy_configured_does_not_warn(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.WARNING):
        policy = load_policy("s3cr3t-admin-token")
    assert policy.is_configured is True
    assert policy.token == "s3cr3t-admin-token"
    assert caplog.text == ""


def test_admin_path_prefix_is_the_admin_surface() -> None:
    assert ADMIN_PATH_PREFIX == "/api/v1/admin/"


# --- configured mode ---------------------------------------------------------


def test_is_authorized_missing_header_returns_false() -> None:
    assert is_authorized(CONFIGURED, None) is False
    assert is_authorized(CONFIGURED, "") is False


def test_is_authorized_wrong_prefix_returns_false() -> None:
    assert is_authorized(CONFIGURED, "Token s3cr3t-admin-token") is False
    assert is_authorized(CONFIGURED, "Basic s3cr3t-admin-token") is False
    assert is_authorized(CONFIGURED, "bearer s3cr3t-admin-token") is False  # case-sensitive scheme
    assert is_authorized(CONFIGURED, "s3cr3t-admin-token") is False  # no scheme


def test_is_authorized_empty_presented_token_returns_false() -> None:
    assert is_authorized(CONFIGURED, "Bearer ") is False
    assert is_authorized(CONFIGURED, "Bearer") is False


def test_is_authorized_correct_token_returns_true() -> None:
    assert is_authorized(CONFIGURED, "Bearer s3cr3t-admin-token") is True


def test_is_authorized_wrong_token_returns_false() -> None:
    """Deliberately different LENGTH as well as different bytes.

    ``hmac.compare_digest`` returns False for a length mismatch just as ``==``
    does, but pairing the two cases here (this one and the prefix-only case)
    keeps the intent explicit: no length-based or early-exit shortcut may grant
    access.
    """
    # same length, different bytes
    assert is_authorized(CONFIGURED, "Bearer WRONG-admin-token") is False
    # shorter
    assert is_authorized(CONFIGURED, "Bearer s3cr3t") is False
    # longer
    assert is_authorized(CONFIGURED, "Bearer s3cr3t-admin-token-extra") is False
    # single-char difference (classic constant-time probe)
    assert is_authorized(CONFIGURED, "Bearer s3cr3t-admin-tokeX") is False


def test_is_authorized_uses_compare_digest(monkeypatch: pytest.MonkeyPatch) -> None:
    """Guard rail: the equality check must stay hmac.compare_digest."""
    import ksma.shared.auth.admin_token as mod

    calls: list[tuple[str, str]] = []
    real = mod.hmac.compare_digest

    def spy(a: str, b: str) -> bool:  # type: ignore[no-untyped-def]
        calls.append((a, b))
        return real(a, b)

    monkeypatch.setattr(mod.hmac, "compare_digest", spy)
    assert is_authorized(CONFIGURED, "Bearer s3cr3t-admin-token") is True
    assert calls == [("s3cr3t-admin-token", "s3cr3t-admin-token")]


def test_compare_digest_raises_on_mixed_types() -> None:
    """compare_digest refuses str/bytes mixes — headers are always str here."""
    import hmac

    with pytest.raises(TypeError):
        hmac.compare_digest(b"abc", "abc")  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Issue #37 — pin the policy surface + add revocation-style assertions.
#
# The current module exposes a *shared-secret* token (per ADR-0007), not a
# JWT — there is no `iss` / `role` / `exp` field on the token itself. The
# "payload" pinned below is the snapshot returned by ``load_policy`` (the
# `token` + `is_configured` fields), and the "wrong-issuer" / "revoked"
# assertions exercise the *behaviour* of ``is_authorized`` against the same
# kinds of header errors an attacker would produce. We cannot change the
# source, so the "revoked" case is the falsy/wrong/empty token matrix.
# ---------------------------------------------------------------------------


class TestAdminPolicyPayload:
    """Pin the ``AdminTokenPolicy`` snapshot fields exactly."""

    def test_load_policy_returns_token_unchanged(self) -> None:
        # The token is stored verbatim — no trimming, no lower-casing, no
        # encoding. A `strip()` or `lower()` mutation on the policy would
        # break this.
        p = load_policy("MIXEDcase-Token-1234")
        assert p.token == "MIXEDcase-Token-1234"

    def test_load_policy_is_configured_for_any_truthy_value(self) -> None:
        # Any non-empty string flips the flag on; an `if token is None` flip
        # would reject the truthy "x" string here.
        p = load_policy("x")
        assert p.is_configured is True
        assert p.token == "x"

    def test_load_policy_is_configured_false_for_empty_string(self) -> None:
        # Empty / explicit-None are both "unconfigured". A `if not token` flip
        # would correctly catch both; we lock both in.
        assert load_policy("").is_configured is False
        assert load_policy(None).is_configured is False

    def test_load_policy_token_field_is_none_when_unconfigured(
        self,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        # When the policy is "dev mode", the `token` field must be `None`,
        # not the empty string. This is what lets `is_authorized` short-circuit.
        with caplog.at_level(logging.WARNING, logger="ksma.shared.auth.admin_token"):
            p = load_policy("")
        assert p.token is None


class TestIsAuthorizedNegativeCases:
    """Issue #37 — reject wrong-issuer / wrong-secret / revoked-token shapes.

    The admin token is a shared secret, not a JWT — so "wrong issuer" maps
    to "wrong bytes", "revoked" maps to "token that should not pass". The
    matrix below pins every way the middleware should refuse.
    """

    @pytest.mark.parametrize(
        "header",
        [
            "Bearer ",               # empty presented token
            "Bearer",                # missing space
            "Bearer  s3cr3t-admin-token",  # double space
            "Bearer\ts3cr3t-admin-token",  # tab instead of space
            "bearer s3cr3t-admin-token",   # wrong case scheme
            "BEARER s3cr3t-admin-token",   # wrong case scheme
            "Token s3cr3t-admin-token",    # wrong scheme name
            "Basic s3cr3t-admin-token",    # wrong scheme name
            "s3cr3t-admin-token",          # no scheme at all
            "S3CR3T-ADMIN-TOKEN",          # upper-case presented token
            "Bearer s3cr3t-admin-toke",    # truncated
            "Bearer s3cr3t-admin-tokens",  # trailing extra byte
            "Bearer  s3cr3t-admin-token ", # trailing whitespace
        ],
    )
    def test_is_authorized_rejects_malformed_or_wrong_token(
        self, header: str
    ) -> None:
        # An `==` flip or a missing `not` in the prefix check would let at
        # least one of these slip through. Every case below MUST be False.
        assert is_authorized(CONFIGURED, header) is False

    def test_is_authorized_rejects_none_header(self) -> None:
        # The configured path must explicitly reject the None header (the
        # unconfigured path explicitly accepts it). A `not header` flip
        # would conflate the two paths.
        assert is_authorized(CONFIGURED, None) is False
        # And again — the *unconfigured* path accepts None. Pin both so
        # the asymmetry is observable.
        assert is_authorized(UNCONFIGURED, None) is True

    def test_is_authorized_returns_exact_bool(self) -> None:
        # Pin the return type as `bool`, not truthy. A `return presented`
        # mutation would leak the raw string and break this assertion.
        result = is_authorized(CONFIGURED, "Bearer s3cr3t-admin-token")
        assert result is True
        assert isinstance(result, bool)
        result = is_authorized(CONFIGURED, "Bearer wrong")
        assert result is False
        assert isinstance(result, bool)


class TestCompareDigestAlwaysInvoked:
    """Lock in the constant-time compare — no path may early-exit on equality."""

    def test_constant_time_compare_runs_even_for_same_length(self, monkeypatch) -> None:
        # An `==` short-circuit mutation would NOT call compare_digest; the
        # spy below would record zero calls.
        import ksma.shared.auth.admin_token as mod

        calls: list[tuple[str, str]] = []
        real = mod.hmac.compare_digest

        def spy(a: str, b: str) -> bool:
            calls.append((a, b))
            return real(a, b)

        monkeypatch.setattr(mod.hmac, "compare_digest", spy)
        # Same-length, different bytes — classic constant-time probe.
        is_authorized(CONFIGURED, "Bearer WRONG-admin-token")
        assert len(calls) == 1
        a, b = calls[0]
        assert a == "WRONG-admin-token"
        assert b == "s3cr3t-admin-token"

    def test_constant_time_compare_runs_even_for_different_length(
        self, monkeypatch
    ) -> None:
        # The docstring warns: the `len(...) == len(...)` shortcut is
        # *not* a secret-dependent compare. compare_digest MUST run for any
        # length mismatch too. A `return False` early-exit mutation would
        # miss this.
        import ksma.shared.auth.admin_token as mod

        calls: list[tuple[str, str]] = []
        real = mod.hmac.compare_digest

        def spy(a: str, b: str) -> bool:
            calls.append((a, b))
            return real(a, b)

        monkeypatch.setattr(mod.hmac, "compare_digest", spy)
        is_authorized(CONFIGURED, "Bearer s3cr3t")  # shorter
        assert len(calls) == 1
        is_authorized(CONFIGURED, "Bearer s3cr3t-admin-token-extra")  # longer
        assert len(calls) == 2