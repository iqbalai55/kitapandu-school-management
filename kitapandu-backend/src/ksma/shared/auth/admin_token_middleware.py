"""FastAPI middleware gating ``/api/v1/admin/*`` behind ``KITAPANDU_ADMIN_TOKEN``.

Behaviour (ADR-0007):

- **Unconfigured** (``KITAPANDU_ADMIN_TOKEN`` unset/empty, i.e. dev): the
  WARNING was already logged once at startup by
  :func:`ksma.shared.auth.admin_token.load_policy`; every request passes
  through.
- **Configured**: ``Authorization: Bearer <token>`` is compared against the
  configured token in constant time. On a miss we return ``401`` with a fixed
  envelope and never echo the presented or expected token.

The policy is read ONCE at app creation (``ksma.main.create_app``) and stashed
on ``app.state.admin_token_policy``; this middleware only reads it.
"""
from __future__ import annotations

import logging
import uuid
from typing import Any

from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from ksma.shared.auth.admin_token import (
    ADMIN_PATH_PREFIX,
    HEADER_NAME,
    AdminTokenPolicy,
    is_authorized,
    load_policy,
)

logger = logging.getLogger(__name__)

#: Unconfigured (dev) fallback — used only if the middleware runs against an app
#: that never went through ``create_app``'s startup wiring.
DEFAULT_POLICY = AdminTokenPolicy(token=None)


def get_policy(app: Any) -> AdminTokenPolicy:
    """Read the policy stashed on ``app.state`` (falls back to dev mode)."""
    state = getattr(app, "state", None)
    policy = getattr(state, "admin_token_policy", None) if state is not None else None
    return policy if isinstance(policy, AdminTokenPolicy) else DEFAULT_POLICY


def unauthorized_response() -> JSONResponse:
    """Fixed 401 envelope. No payload leak: never echo the presented token."""
    return JSONResponse(
        status_code=401,
        content={
            "code": "ADMIN_TOKEN_REQUIRED",
            "message": "Admin endpoint requires a valid KITAPANDU_ADMIN_TOKEN bearer credential.",
            "correlation_id": str(uuid.uuid4()),
        },
    )


class AdminTokenMiddleware(BaseHTTPMiddleware):
    """Gate every ``/api/v1/admin/*`` request behind the admin token."""

    async def dispatch(
        self, request: Request, call_next: RequestResponseEndpoint
    ) -> Response:
        if request.url.path.startswith(ADMIN_PATH_PREFIX):
            policy = get_policy(request.app)
            if not is_authorized(policy, request.headers.get(HEADER_NAME)):
                logger.info(
                    "Rejected unauthenticated admin request: %s %s",
                    request.method,
                    request.url.path,
                )
                return unauthorized_response()
        return await call_next(request)


def init_admin_token_policy(app: Any, token: str | None) -> AdminTokenPolicy:
    """Build the policy once at startup and stash it on ``app.state``.

    Returns the policy so the caller (and tests) can assert on it.
    """
    policy = load_policy(token)
    app.state.admin_token_policy = policy
    if policy.is_configured:
        logger.info("Admin token gate enabled for /api/v1/admin/*.")
    return policy


__all__ = [
    "AdminTokenMiddleware",
    "get_policy",
    "init_admin_token_policy",
    "unauthorized_response",
]