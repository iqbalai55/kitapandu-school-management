"""Argon2id + JWT + refresh tokens + recovery tokens + FastAPI deps.

Per ADR0001 §7 + Q08 deferral:

- Passwords hashed with argon2id via ``argon2-cffi`` (not passlib).
- Access JWT (15 min default) carries identity only, NOT grants — grants are
  loaded fresh on each request by the resolver.
- Refresh token (token stored as a SHA-256 hash in ``refresh_token``
  table; revocable + chainable via ``replaced_by_id``.
- ``Account.authz_revision`` increments on rights changes; access JWTs embed
  ``authz_revision`` and the resolver compares it with the current DB value.
- Recovery via time-bounded single-use token (out of band: log to notification in
  pilot, since Q09 external channel not chosen).
- FastAPI dependencies in ``ksma.shared.auth.dependencies`` resolve the JWT
  into a TenantContext and run whole-grant RBAC per route.
"""
from __future__ import annotations

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerifyMismatchError

from ksma.shared.errors import InvalidCredentialsError

_hasher = PasswordHasher()  # argon2id, RFC 9106 low-memory defaults


def hash_password(plaintext: str) -> str:
    return _hasher.hash(plaintext)


def verify_password(stored_hash: str, plaintext: str) -> bool:
    try:
        _hasher.verify(stored_hash, plaintext)
        return True
    except VerifyMismatchError:
        return False
    except InvalidHashError:
        return False


def needs_rehash(stored_hash: str) -> bool:
    return _hasher.check_needs_rehash(stored_hash)


def raise_invalid_credentials() -> None:
    """Single point of failure — never leak *why* credentials are wrong."""
    raise InvalidCredentialsError("Invalid credentials")