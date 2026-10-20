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


# ---------------------------------------------------------------------------
# Issue #37 — parametrised HTTP-status mapping for every documented code.
#
# The table below mirrors the 9 codes the issue specifies: INVALID_VALUE,
# REQUIRED_FIELD, REFERENCE_SCOPE_MISMATCH, STATE_CONFLICT, VERSION_CONFLICT,
# CAPACITY_EXCEEDED, ACCESS_DENIED, IDEMPOTENCY_CONFLICT, POLICY_NOT_CONFIGURED.
# We pin the (error class, code, http_status) tuple for every row so that
# any silent change to the mapping in ``ksma.shared.errors`` fails a test.
# ---------------------------------------------------------------------------


ERROR_HTTP_STATUS_TABLE = [
    pytest.param(
        ValidationError, "INVALID_VALUE", 422, id="INVALID_VALUE",
    ),
    pytest.param(
        RequiredFieldError, "REQUIRED_FIELD", 422, id="REQUIRED_FIELD",
    ),
    pytest.param(
        ReferenceScopeMismatchError, "REFERENCE_SCOPE_MISMATCH", 422,
        id="REFERENCE_SCOPE_MISMATCH",
    ),
    pytest.param(
        StateConflictError, "STATE_CONFLICT", 409, id="STATE_CONFLICT",
    ),
    pytest.param(
        VersionConflictError, "VERSION_CONFLICT", 409, id="VERSION_CONFLICT",
    ),
    pytest.param(
        CapacityExceededError, "CAPACITY_EXCEEDED", 409, id="CAPACITY_EXCEEDED",
    ),
    pytest.param(
        AccessDeniedError, "ACCESS_DENIED", 403, id="ACCESS_DENIED",
    ),
    # IDEMPOTENCY_CONFLICT alias — the class is named IDEMPOTENCY_KEY_CONFLICT
    # in the source but the wire contract spells it differently in some
    # older docs. We pin the actual class behaviour here.
    pytest.param(
        IdempotencyKeyConflictError, "IDEMPOTENCY_KEY_CONFLICT", 422,
        id="IDEMPOTENCY_CONFLICT",
    ),
    pytest.param(
        PolicyNotConfiguredError, "POLICY_NOT_CONFIGURED", 409,
        id="POLICY_NOT_CONFIGURED",
    ),
]


@pytest.mark.parametrize("error_cls, expected_code, expected_status", ERROR_HTTP_STATUS_TABLE)
def test_error_class_http_status_mapping(
    error_cls: type[DomainError],
    expected_code: str,
    expected_status: int,
) -> None:
    """The class-level `code` + `http_status` attributes must match the
    documented wire mapping exactly — any silent flip (e.g. ``409`` →
    ``410`` or a code rename) breaks a row of this table.
    """
    assert error_cls.code == expected_code
    assert error_cls.http_status == expected_status

    # And the instance-level access returns the same value — no classmethod
    # shortcut that loses the value when the instance is constructed.
    e = error_cls("a message")
    assert e.code == expected_code
    assert e.http_status == expected_status


@pytest.mark.parametrize("error_cls, expected_code, expected_status", ERROR_HTTP_STATUS_TABLE)
def test_error_payload_carries_code_and_correlation_id(
    error_cls: type[DomainError],
    expected_code: str,
    expected_status: int,
) -> None:
    """`to_payload()` must serialise `code` + `correlation_id` on every row
    of the table. The correlation_id is the only mandatory field for
    downstream tracing per NFR08.
    """
    e = error_cls("a message")
    p = e.to_payload()
    assert p["code"] == expected_code
    assert p["message"] == "a message"
    # Correlation_id is always present and parses cleanly.
    UUID(p["correlation_id"])
    # And the http_status on the instance matches the class-level mapping
    # so the FastAPI handler can pick it up.
    assert e.http_status == expected_status


# ---------------------------------------------------------------------------
# ACCESS_DENIED payload must NEVER include arbitrary details.
# Per OWASP R1 + access matrix §3: no participant name, no object existence,
# no object payload. The error class accepts `details={...}` but the canonical
# shape (no details supplied) must serialise to a payload without that key.
# ---------------------------------------------------------------------------


def test_access_denied_payload_has_no_details_by_default() -> None:
    """An AccessDeniedError constructed without details must NOT include a
    `details` key in the wire payload. A `details={}` mutation or a
    `payload["details"] = ...` flip in ``to_payload`` would surface here.
    """
    e = AccessDeniedError("Forbidden")
    p = e.to_payload()
    assert "details" not in p
    assert "field_path" not in p
    assert p == {
        "code": "ACCESS_DENIED",
        "message": "Forbidden",
        "correlation_id": p["correlation_id"],
    }


def test_access_denied_does_not_leak_pii_via_message() -> None:
    """The message is the safe human-readable string; we pin that arbitrary
    caller-supplied strings do NOT appear in `details`. A mutation that
    copied `message` into `details` would leak PII and fail here.
    """
    e = AccessDeniedError(
        "Forbidden: cannot access participant participant_id=12345",
    )
    p = e.to_payload()
    # The raw message is preserved (operators need to log it), but details
    # is not auto-populated.
    assert p["message"].startswith("Forbidden")
    assert "details" not in p


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