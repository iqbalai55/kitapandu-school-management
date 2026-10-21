"""Admin-token policy for ``/api/v1/admin/*`` routes (ADR-0007).

When ``KITAPANDU_ADMIN_TOKEN`` is set (production / staging), every
``/api/v1/admin/*`` request must carry ``Authorization: Bearer <token>``.
The compare is constant-time (:func:`hmac.compare_digest`) so a caller cannot
recover the token by measuring response latency.

When the env var is unset or empty (dev / local), :func:`load_policy` logs a
WARNING **once** and returns an unconfigured policy; every request is then
allowed through so local development stays frictionless.

The middleware that consumes this module lives in
``ksma.shared.auth.admin_token_middleware``.
"""
from __future__ import annotations

import hmac
import logging
from dataclasses import dataclass
from typing import Final

logger = logging.getLogger(__name__)

#: Path prefix the middleware guards. Anything under it requires the token.
ADMIN_PATH_PREFIX: Final[str] = "/api/v1/admin/"

HEADER_NAME: Final[str] = "authorization"
BEARER_PREFIX: Final[str] = "Bearer "


@dataclass(frozen=True, slots=True)
class AdminTokenPolicy:
    """Snapshot of the admin-token config used by the middleware.

    T30 D3: the previous two-field design (token + is_configured) had a
    latent invariant gap — nothing prevented a caller constructing
    ``AdminTokenPolicy(is_configured=True, token=None)`` and ending up
    with a silent bypass. ``is_configured`` is now derived as
    ``token is not None``; the inconsistent state is unrepresentable.
    """

    token: str | None  # None = dev/no-token mode

    @property
    def is_configured(self) -> bool:
        return self.token is not None


def load_policy(token: str | None) -> AdminTokenPolicy:
    """Read the env var; log a WARNING if it's unset (dev-style).

    Called ONCE per app start (not per request) so the "admin surface is open"
    warning is a one-shot line in the startup log, not per-request noise.
    """
    if not token:
        logger.warning(
            "KITAPANDU_ADMIN_TOKEN is unset — /api/v1/admin/* is OPEN. "
            "Set the env var in any non-dev environment."
        )
        return AdminTokenPolicy(token=None)
    return AdminTokenPolicy(token=token)


def is_authorized(policy: AdminTokenPolicy, authorization_header: str | None) -> bool:
    """Constant-time compare of the supplied Bearer token vs the configured one.

    Returns True when the policy is not configured (dev mode) — that is the
    documented "frictionless local dev" behaviour, not a bypass bug.

    Note: the length check below is *not* a secret-dependent comparison; it only
    rejects obviously malformed headers before the constant-time compare. The
    actual equality check is always ``hmac.compare_digest``.
    """
    if not policy.is_configured or policy.token is None:
        return True
    if not authorization_header:
        return False
    if not authorization_header.startswith(BEARER_PREFIX):
        return False
    presented = authorization_header[len(BEARER_PREFIX):]
    if not presented:
        return False
    return hmac.compare_digest(presented, policy.token)