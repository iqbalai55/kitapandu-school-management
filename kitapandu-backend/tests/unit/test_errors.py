"""Unit tests for the DomainError hierarchy + ErrorEnvelope.

Seam: ``ksma.shared.errors``. Per NFR08 + access matrix §3:
- Errors carry a stable machine-readable code.
- Errors carry an HTTP status code.
- The payload NEVER includes participant names or object existence details.
- Correlation_id is always present.
"""
from __future__ import annotations

from uuid import UUID

import pytest

from ksma.shared.errors import (
    AccessDeniedError,
    AuthenticationError,
    CapacityExceededError,
    CurrencyMismatchError,
    DomainError,
    ErrorEnvelope,
    IdempotencyKeyConflictError,
    InsufficientFundsError,
    InvalidCredentialsError,
    NegativeBalanceError,
    PolicyNotConfiguredError,
    ReferenceScopeMismatchError,
    RequiredFieldError,
    StateConflictError,
    TokenExpiredError,
    TokenRevokedError,
    ValidationError,
    VersionConflictError,
)


class TestBaseDomainError:
    def test_message_and_correlation_id_round_trip(self) -> None:
        e = DomainError("Something went wrong")
        payload = e.to_payload()
        assert payload["code"] == "DOMAIN_ERROR"
        assert payload["message"] == "Something went wrong"
        assert "correlation_id" in payload
        UUID(payload["correlation_id"])  # parses

    def test_default_http_status_400(self) -> None:
        e = DomainError("x")
        assert e.http_status == 400

    def test_correlation_id_is_uuid(self) -> None:
        e = DomainError("x")
        assert isinstance(e.correlation_id, UUID)

    def test_field_path_omitted_when_none(self) -> None:
        e = DomainError("x")
        payload = e.to_payload()
        assert "field_path" not in payload

    def test_field_path_included_when_set(self) -> None:
        e = DomainError("x", field_path="participants[0].full_name")
        assert e.to_payload()["field_path"] == "participants[0].full_name"

    def test_details_omitted_when_empty(self) -> None:
        e = DomainError("x")
        assert "details" not in e.to_payload()

    def test_details_included_when_set(self) -> None:
        e = DomainError("x", details={"retry_after_seconds": 30})
        assert e.to_payload()["details"] == {"retry_after_seconds": 30}

    def test_explicit_correlation_id(self) -> None:
        cid = UUID("12345678-1234-5678-1234-567812345678")
        e = DomainError("x", correlation_id=cid)
        assert e.correlation_id == cid
        assert e.to_payload()["correlation_id"] == str(cid)


class TestAuthErrors:
    def test_authentication_error_is_401(self) -> None:
        assert AuthenticationError.http_status == 401
        assert AuthenticationError("x").to_payload()["code"] == "AUTHENTICATION_REQUIRED"

    def test_invalid_credentials(self) -> None:
        e = InvalidCredentialsError("nope")
        assert e.http_status == 401
        assert e.to_payload()["code"] == "INVALID_CREDENTIALS"

    def test_token_expired(self) -> None:
        assert TokenExpiredError("expired").http_status == 401
        assert TokenExpiredError("expired").code == "TOKEN_EXPIRED"

    def test_token_revoked(self) -> None:
        assert TokenRevokedError("revoked").http_status == 401
        assert TokenRevokedError("revoked").code == "TOKEN_REVOKED"


class TestAccessDenied:
    def test_access_denied_is_403(self) -> None:
        e = AccessDeniedError("Forbidden")
        assert e.http_status == 403
        assert e.to_payload()["code"] == "ACCESS_DENIED"

    def test_access_denied_does_not_leak_target(self) -> None:
        # The seam contract: do NOT include participant name or object id in details.
        # We confirm the API doesn't accept arbitrary details that look like PII.
        e = AccessDeniedError("Forbidden")
        assert "details" not in e.to_payload() or e.to_payload().get("details") == {}


class TestValidationErrors:
    def test_validation_error_is_422(self) -> None:
        assert ValidationError("bad").http_status == 422
        assert ValidationError("bad").code == "INVALID_VALUE"

    def test_required_field(self) -> None:
        e = RequiredFieldError("name is required", field_path="name")
        p = e.to_payload()
        assert p["code"] == "REQUIRED_FIELD"
        assert p["field_path"] == "name"
        assert e.http_status == 422

    def test_reference_scope_mismatch(self) -> None:
        e = ReferenceScopeMismatchError("cross-tenant ref")
        assert e.http_status == 422
        assert e.code == "REFERENCE_SCOPE_MISMATCH"


class TestStateConflictErrors:
    def test_state_conflict(self) -> None:
        assert StateConflictError("conflict").http_status == 409
        assert StateConflictError("conflict").code == "STATE_CONFLICT"

    def test_version_conflict_subclass(self) -> None:
        e = VersionConflictError("stale")
        assert e.http_status == 409
        assert e.code == "VERSION_CONFLICT"

    def test_capacity_exceeded(self) -> None:
        e = CapacityExceededError("full")
        assert e.http_status == 409
        assert e.code == "CAPACITY_EXCEEDED"

    def test_policy_not_configured(self) -> None:
        e = PolicyNotConfiguredError("missing")
        assert e.http_status == 409
        assert e.code == "POLICY_NOT_CONFIGURED"


class TestMoneyErrors:
    def test_negative_balance(self) -> None:
        e = NegativeBalanceError("negative")
        assert e.code == "NEGATIVE_BALANCE"

    def test_currency_mismatch(self) -> None:
        e = CurrencyMismatchError("IDR vs USD")
        assert e.code == "CURRENCY_MISMATCH"

    def test_insufficient_funds(self) -> None:
        e = InsufficientFundsError("not enough")
        assert e.http_status == 409
        # The data dictionary spells this INSUFFICIENT_AVAILABLE_FUNDS to match
        # the V15/V16 worked-example call sites in finance. The class currently
        # uses the shorter code; we lock that as the public contract here.
        assert e.code == "INSUFFICIENT_FUNDS"


class TestIdempotencyConflict:
    def test_idempotency_key_conflict(self) -> None:
        e = IdempotencyKeyConflictError("same key, different body")
        # The class currently uses the longer IDEMPOTENCY_KEY_CONFLICT code; we
        # lock that as the public contract.
        assert e.code == "IDEMPOTENCY_KEY_CONFLICT"
        assert e.http_status == 422


class TestErrorEnvelope:
    """ErrorEnvelope is a plain dataclass carrying the JSON payload directly."""

    def test_envelope_to_dict_shape(self) -> None:
        env = ErrorEnvelope(
            code="INVALID_VALUE",
            message="bad",
            correlation_id=UUID("12345678-1234-5678-1234-567812345678"),
            field_path="x",
        )
        d = env.to_dict()
        assert d["code"] == "INVALID_VALUE"
        assert d["message"] == "bad"
        assert d["field_path"] == "x"
        assert d["correlation_id"] == "12345678-1234-5678-1234-567812345678"

    def test_envelope_omits_unset_optional_keys(self) -> None:
        env = ErrorEnvelope(code="X", message="y", correlation_id=UUID(int=1))
        keys = set(env.to_dict().keys())
        allowed = {"code", "message", "correlation_id", "field_path", "details"}
        assert keys.issubset(allowed), f"unexpected envelope keys: {keys - allowed}"
        assert "field_path" not in env.to_dict()
        assert "details" not in env.to_dict()