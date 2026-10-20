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