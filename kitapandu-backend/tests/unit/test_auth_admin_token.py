"""Unit tests for ``ksma.shared.auth.admin_token`` (ADR-0007).

Seam: the admin-token *policy* layer:
- ``ADMIN_PATH_PREFIX`` / ``HEADER_NAME`` / ``BEARER_PREFIX`` — the constants
  the middleware matches on.
- ``load_policy(token)`` — one-shot startup read of ``KITAPANDU_ADMIN_TOKEN``;
  logs a WARNING exactly once when unconfigured.
- ``is_authorized(policy, header)`` — the constant-time
  (``hmac.compare_digest``) decision used by the middleware.

Behaviour asserted: the configured/unconfigured decision matrix, the
frictionless dev-mode bypass, and that no malformed header is ever accepted.
No FastAPI app, no network; the middleware's own dispatch is covered by
``test_auth_admin_token_middleware.py``.
"""
from __future__ import annotations

import logging
from typing import Any

import pytest

from ksma.shared.auth.admin_token import (
    ADMIN_PATH_PREFIX,
    BEARER_PREFIX,
    HEADER_NAME,
    AdminTokenPolicy,
    is_authorized,
    load_policy,
)

TOKEN = "s3cr3t-admin-token"


class TestConstants:
    def test_path_prefix_guards_the_admin_surface(self) -> None:
        assert ADMIN_PATH_PREFIX == "/api/v1/admin/"
        assert ADMIN_PATH_PREFIX.startswith("/api/v1/")

    def test_header_and_scheme_names(self) -> None:
        assert HEADER_NAME == "authorization"
        assert BEARER_PREFIX == "Bearer "


class TestLoadPolicy:
    @pytest.mark.parametrize("token", ["a-token", "x" * 64])
    def test_configured_token_yields_configured_policy(self, token: str) -> None:
        policy = load_policy(token)
        assert policy.is_configured is True
        assert policy.token == token

    @pytest.mark.parametrize("empty", [None, ""], ids=["unset", "empty"])
    def test_unset_token_yields_dev_policy_and_warns_once(
        self, empty: str | None, caplog: pytest.LogCaptureFixture
    ) -> None:
        with caplog.at_level(logging.WARNING):
            policy = load_policy(empty)
        assert policy.is_configured is False
        assert policy.token is None
        warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert len(warnings) == 1, "the open-admin-surface warning must be one-shot"
        assert "/api/v1/admin/*" in warnings[0].getMessage()

    def test_configured_policy_does_not_warn(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        with caplog.at_level(logging.WARNING):
            load_policy(TOKEN)
        assert [r for r in caplog.records if r.levelno == logging.WARNING] == []


class TestIsAuthorized:
    # --- dev mode -----------------------------------------------------------

    def test_unconfigured_policy_allows_everything(self) -> None:
        """Frictionless local dev: the admin surface is open, no header needed."""
        assert is_authorized(load_policy(None), None) is True
        assert is_authorized(load_policy(""), "Bearer wrong") is True

    def test_configured_but_tokenless_policy_still_allows(
        self,
    ) -> None:
        """Kills the ``or -> and`` mutant on line 64.

        ``AdminTokenPolicy(is_configured=True, token=None)`` is the
        half-configured state (e.g. a caller built the dataclass by hand). The
        documented dev-mode bypass is ``not configured OR no token`` — so this
        combination must allow the request through. Under ``and`` the code
        would fall through to ``hmac.compare_digest(presented, None)`` and
        raise ``TypeError`` instead of allowing.
        """
        half = AdminTokenPolicy(token=None, is_configured=True)
        assert half.is_configured is True and half.token is None
        assert is_authorized(half, None) is True
        assert is_authorized(half, "Bearer anything") is True

    # --- configured mode ---------------------------------------------------

    def test_exact_bearer_token_is_authorized(self) -> None:
        assert is_authorized(load_policy(TOKEN), f"Bearer {TOKEN}") is True

    @pytest.mark.parametrize(
        "header",
        [
            pytest.param(None, id="missing_header"),
            pytest.param("", id="empty_header"),
            pytest.param(TOKEN, id="no_bearer_prefix"),
            pytest.param(f"bearer {TOKEN}", id="lowercase_scheme"),
            pytest.param(f"Basic {TOKEN}", id="wrong_scheme"),
            pytest.param("Bearer ", id="empty_credential"),
            pytest.param("Bearer wrong-token", id="wrong_token"),
            pytest.param(f"Bearer {TOKEN}-extra", id="token_prefix"),
            pytest.param(f"Bearer {TOKEN[:-1]}", id="token_truncated"),
        ],
    )
    def test_malformed_or_mismatched_header_is_rejected(self, header: str | None) -> None:
        assert is_authorized(load_policy(TOKEN), header) is False

    def test_compare_is_constant_time_not_plain_equality(self) -> None:
        """The decision is delegated to ``hmac.compare_digest``; patch it to
        prove it is the thing actually deciding (timing-attack defence)."""
        import ksma.shared.auth.admin_token as mod  # noqa: PLC0415

        seen: list[tuple[str, str]] = []

        def _spy(a: str, b: str) -> bool:
            seen.append((a, b))
            return a == b

        policy = load_policy(TOKEN)
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(mod.hmac, "compare_digest", _spy)
            assert mod.is_authorized(policy, f"Bearer {TOKEN}") is True
        assert seen == [(TOKEN, TOKEN)], (
            f"is_authorized must use hmac.compare_digest, called {seen!r}"
        )