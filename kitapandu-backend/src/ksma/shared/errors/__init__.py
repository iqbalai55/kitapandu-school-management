"""Domain errors → HTTP mapping.

Per NFR08 + the UX Blueprint state catalogue, every error response has the shape::

    {
      "code": "MACHINE_READABLE_CODE",
      "message": "human language safe to show to the user",
      "field_path": "optional JSONPath-like string",
      "correlation_id": "uuid"
    }

Never leak participant names, object existence, or object payloads in error
responses (OWASP R1 / R2, default-deny principle from KSMA Access Matrix §3).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from uuid import UUID, uuid4


class DomainError(Exception):
    """Base for all KSMA domain errors.

    Subclasses set ``code`` (stable, machine-readable), ``http_status`` (default 400),
    and optionally ``field_path``.
    """

    code: str = "DOMAIN_ERROR"
    http_status: int = 400

    def __init__(
        self,
        message: str,
        *,
        field_path: str | None = None,
        details: dict[str, Any] | None = None,
        correlation_id: UUID | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.field_path = field_path
        self.details = details or {}
        self.correlation_id = correlation_id or uuid4()

    def to_payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "code": self.code,
            "message": self.message,
            "correlation_id": str(self.correlation_id),
        }
        if self.field_path:
            payload["field_path"] = self.field_path
        if self.details:
            payload["details"] = self.details
        return payload


# --- Auth & access (mapped to 401 / 403) ---


class AuthenticationError(DomainError):
    code = "AUTHENTICATION_REQUIRED"
    http_status = 401


class InvalidCredentialsError(AuthenticationError):
    code = "INVALID_CREDENTIALS"
    http_status = 401


class TokenExpiredError(AuthenticationError):
    code = "TOKEN_EXPIRED"
    http_status = 401


class TokenRevokedError(AuthenticationError):
    code = "TOKEN_REVOKED"
    http_status = 401


class AuthzRevisionMismatchError(AuthenticationError):
    code = "AUTHZ_REVISION_MISMATCH"
    http_status = 401


class AccessDeniedError(DomainError):
    code = "ACCESS_DENIED"
    http_status = 403


# --- Validation (mapped to 422) ---


class ValidationError(DomainError):
    code = "INVALID_VALUE"
    http_status = 422


class RequiredFieldError(ValidationError):
    code = "REQUIRED_FIELD"


class ReferenceScopeMismatchError(ValidationError):
    code = "REFERENCE_SCOPE_MISMATCH"


class StateConflictError(DomainError):
    code = "STATE_CONFLICT"
    http_status = 409


class VersionConflictError(StateConflictError):
    code = "VERSION_CONFLICT"
    http_status = 409


class CapacityExceededError(StateConflictError):
    code = "CAPACITY_EXCEEDED"
    http_status = 409


class PolicyNotConfiguredError(StateConflictError):
    """Raised when a Q-decision policy is still in `draft` status.

    Per Q01–Q14, no operational flow may run on real data until a ``PolicyVersion``
    with ``status='published'`` exists for that domain.
    """

    code = "POLICY_NOT_CONFIGURED"
    http_status = 409


# --- Money (mapped to 409 / 422) ---


class InsufficientFundsError(StateConflictError):
    code = "INSUFFICIENT_FUNDS"
    http_status = 409


class NegativeBalanceError(DomainError):
    code = "NEGATIVE_BALANCE"
    http_status = 422


class CurrencyMismatchError(DomainError):
    code = "CURRENCY_MISMATCH"
    http_status = 422


# --- Idempotency (mapped to 409 / 422) ---


class IdempotencyKeyConflictError(DomainError):
    """Same key, different request body (R21)."""

    code = "IDEMPOTENCY_KEY_CONFLICT"
    http_status = 409


# --- Operational (mapped to 500 / 503) ---


class OperationalError(DomainError):
    code = "OPERATIONAL_ERROR"
    http_status = 500


class NotImplementedYetError(DomainError):
    """Stub used while a context is being implemented.

    Returned with HTTP 501 by the FastAPI exception handler.
    """

    code = "NOT_IMPLEMENTED"
    http_status = 501


@dataclass(frozen=True, slots=True)
class ErrorEnvelope:
    """Plain dataclass version of the JSON error payload."""

    code: str
    message: str
    correlation_id: UUID
    field_path: str | None = None
    details: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        return DomainError.__dataclass_fields__ and {
            "code": self.code,
            "message": self.message,
            "correlation_id": str(self.correlation_id),
            **({"field_path": self.field_path} if self.field_path else {}),
            **({"details": self.details} if self.details else {}),
        }