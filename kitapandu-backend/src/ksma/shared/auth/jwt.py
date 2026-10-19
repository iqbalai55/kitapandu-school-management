"""JWT issue/decode.

Per ADR0001 §7: HS256 with explicit ``iss`` + ``aud`` claims, ``require`` set so
incomplete tokens are rejected. The token is short-lived (15 min) and carries
only identity + tenant context; the resolver loads fresh grants per request.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import UUID, uuid4

import jwt

from ksma.settings import get_settings
from ksma.shared.errors import TokenExpiredError, TokenRevokedError

_ALG = "HS256"


def issue_access_token(
    *,
    subject: UUID,
    memberships: list[dict[str, Any]],
    current_tenant_id: UUID | None,
    current_unit_id: UUID | None,
    authz_revision: int,
) -> tuple[str, datetime]:
    settings = get_settings()
    now = datetime.now(timezone.utc)
    expires = now + timedelta(seconds=settings.access_token_ttl_seconds)
    payload: dict[str, Any] = {
        "sub": str(subject),
        "iss": settings.jwt_issuer,
        "aud": settings.jwt_audience,
        "iat": now,
        "exp": expires,
        "jti": uuid4().hex,
        # Custom claims (KSMA-specific, namespaced with `tz_` to avoid clashes)
        "tz_memberships": memberships,
        "tz_current_tenant": str(current_tenant_id) if current_tenant_id else None,
        "tz_current_unit": str(current_unit_id) if current_unit_id else None,
        "tz_authz_revision": authz_revision,
    }
    token = jwt.encode(payload, settings.jwt_secret.get_secret_value(), algorithm=_ALG)
    return token, expires


def decode_access_token(token: str) -> dict[str, Any]:
    settings = get_settings()
    try:
        return jwt.decode(
            token,
            settings.jwt_secret.get_secret_value(),
            algorithms=[_ALG],
            audience=settings.jwt_audience,
            issuer=settings.jwt_issuer,
            options={"require": ["exp", "iat", "sub", "aud", "iss"]},
        )
    except jwt.ExpiredSignatureError as exc:
        raise TokenExpiredError("Token expired") from exc
    except jwt.InvalidTokenError as exc:
        raise TokenRevokedError("Invalid or revoked token") from exc


def issue_refresh_token() -> str:
    """Opaque refresh token (256 bits) — stored hashed in DB."""
    import secrets

    return secrets.token_urlsafe(32)