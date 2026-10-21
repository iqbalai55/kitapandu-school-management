"""Unit tests for the FastAPI app factory + global exception handlers.

Seam: ``ksma.main.create_app``. Public surface exercised here:
- the ``DomainError`` handler — status comes from ``exc.http_status`` and the
  body is the error envelope, with NO object payload leaked for ``ACCESS_DENIED``
  (NFR08 in ``docs/specs/000-umbrella-platform.md``);
- the ``RequestValidationError`` handler — the FIRST Pydantic error becomes
  the ``INVALID_VALUE`` envelope (``message`` + dot-joined ``field_path``);
- ``CorrelationIdMiddleware`` is installed on every app (the correlation_id
  contract holds regardless of tracing).

The trigger routes are mounted onto the *real* ``create_app()`` instance so
the assertions run through the genuine handler registry. In-process Starlette
``TestClient`` — no DB, no network, no uvicorn.
"""
from __future__ import annotations

import os
from collections.abc import Iterator

import pytest
from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from fastapi.testclient import TestClient
from pydantic import BaseModel

from ksma.shared.errors import (
    AccessDeniedError,
    CapacityExceededError,
    DomainError,
    IdempotencyKeyConflictError,
    PolicyNotConfiguredError,
    ReferenceScopeMismatchError,
    RequiredFieldError,
    StateConflictError,
    ValidationError,
    VersionConflictError,
)


def _create_app() -> FastAPI:
    """Import ``ksma.main`` lazily: importing it pulls in every router module,
    which read ``Settings()`` at import time — that only works once
    ``_seed_settings_env`` has run."""
    from ksma.main import create_app  # noqa: PLC0415

    return create_app()


@pytest.fixture(autouse=True)
def _seed_settings_env() -> Iterator[None]:
    """Seed the env vars ``Settings()`` requires so ``create_app()`` builds."""
    keys = (
        "KITAPANDU_POSTGRES_DSN",
        "KITAPANDU_POSTGRES_DSN_SYNC",
        "KITAPANDU_JWT_SECRET",
        "KITAPANDU_MINIO_ACCESS_KEY",
        "KITAPANDU_MINIO_SECRET_KEY",
    )
    prev = {k: os.environ.get(k) for k in keys}
    os.environ["KITAPANDU_POSTGRES_DSN"] = "postgresql+asyncpg://x:x@localhost/x"
    os.environ["KITAPANDU_POSTGRES_DSN_SYNC"] = "postgresql://x:x@localhost/x"
    os.environ["KITAPANDU_JWT_SECRET"] = "test-secret-not-used"
    os.environ["KITAPANDU_MINIO_ACCESS_KEY"] = "test-access-key"
    os.environ["KITAPANDU_MINIO_SECRET_KEY"] = "test-secret-key"
    from ksma.settings import get_settings  # noqa: PLC0415

    get_settings.cache_clear()
    try:
        yield
    finally:
        for k, v in prev.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        get_settings.cache_clear()


class _StrictBody(BaseModel):
    amount: int


class _TwoFieldBody(BaseModel):
    amount: int
    currency: str


@pytest.fixture
def client() -> Iterator[TestClient]:
    """The real app plus two trigger routes for the validation handler."""
    app = _create_app()

    @app.post("/_probe/one-required")
    async def _one(required: _StrictBody) -> dict:  # pragma: no cover - 422 first
        return {"ok": True}

    @app.post("/_probe/two-required")
    async def _two(required: _TwoFieldBody) -> dict:  # pragma: no cover - 422 first
        return {"ok": True}

    @app.get("/_probe/two-query-params")
    async def _two_params(
        first_param: int, second_param: int
    ) -> dict:  # pragma: no cover - 422 first
        return {"ok": True}

    @app.get("/_probe/raise-domain-error")
    async def _domain() -> dict:  # pragma: no cover - never returns
        raise AccessDeniedError("nope")

    with TestClient(app, raise_server_exceptions=False) as test_client:
        yield test_client


# ---------------------------------------------------------------------------
# RequestValidationError handler — `first = errors[0] if errors else {}`
# ---------------------------------------------------------------------------


def test_single_validation_error_uses_that_error(client: TestClient) -> None:
    """Exactly ONE error in the list: the handler must read ``errors[0]``.
    Reading ``errors[1]`` raises ``IndexError`` → a 500 instead of a 422."""
    response = client.post("/_probe/one-required", json={})

    assert response.status_code == 422
    body = response.json()
    assert body["code"] == "INVALID_VALUE"
    assert "amount" in body["field_path"]
    assert body["field_path"] == "body.amount"
    assert body["message"] != "Invalid request body"
    assert body["correlation_id"]


def test_two_validation_errors_report_only_the_first(client: TestClient) -> None:
    """The envelope is built from the FIRST error, not the last one."""
    response = client.post("/_probe/two-required", json={})

    assert response.status_code == 422
    body = response.json()
    # Field order in the Pydantic error list follows the model declaration,
    # so `amount` is first and wins — reading errors[1] would report
    # `body.currency` instead.
    assert body["field_path"] == "body.amount"
    assert "currency" not in body["field_path"]


def test_query_param_error_path_is_dot_joined(client: TestClient) -> None:
    """``field_path`` is ``".".join(str(p) for p in loc)``: a query-param error
    reports ``query.<name>``, and only the FIRST of two missing params."""
    response = client.get("/_probe/two-query-params")

    assert response.status_code == 422
    body = response.json()
    assert body["field_path"] == "query.first_param"
    assert "second_param" not in body["field_path"]


def test_empty_error_list_falls_back_to_the_generic_message(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``errors[0] if errors else {}`` — the empty-list arm keeps the generic
    message and an empty ``field_path`` rather than raising ``IndexError``."""
    app = _create_app()

    @app.get("/_probe/empty-errors")
    async def _empty() -> dict:  # pragma: no cover - never returns
        raise RequestValidationError([])

    with TestClient(app, raise_server_exceptions=False) as empty_client:
        response = empty_client.get("/_probe/empty-errors")

    assert response.status_code == 422
    body = response.json()
    assert body["code"] == "INVALID_VALUE"
    assert body["message"] == "Invalid request body"
    assert body["field_path"] == ""


# ---------------------------------------------------------------------------
# DomainError handler — status from the error, payload never leaks
# ---------------------------------------------------------------------------

_DOMAIN_ERROR_CASES = [
    (ValidationError, 422, "INVALID_VALUE"),
    (RequiredFieldError, 422, "REQUIRED_FIELD"),
    (ReferenceScopeMismatchError, 422, "REFERENCE_SCOPE_MISMATCH"),
    (StateConflictError, 409, "STATE_CONFLICT"),
    (VersionConflictError, 409, "VERSION_CONFLICT"),
    (CapacityExceededError, 409, "CAPACITY_EXCEEDED"),
    (AccessDeniedError, 403, "ACCESS_DENIED"),
    (IdempotencyKeyConflictError, 422, "IDEMPOTENCY_KEY_CONFLICT"),
    (PolicyNotConfiguredError, 409, "POLICY_NOT_CONFIGURED"),
]


@pytest.mark.parametrize(
    "error_cls, expected_status, expected_code",
    _DOMAIN_ERROR_CASES,
    ids=[case[2] for case in _DOMAIN_ERROR_CASES],
)
def test_domain_error_http_status_mapping(
    error_cls: type, expected_status: int, expected_code: str
) -> None:
    """Every documented code maps to its documented status through the app's
    global handler (spec #16 user story #20)."""
    app = _create_app()
    instance = error_cls(f"boom {expected_code}")

    @app.get("/_probe/raise")
    async def _raise() -> dict:  # pragma: no cover - never returns
        raise instance

    with TestClient(app, raise_server_exceptions=False) as test_client:
        response = test_client.get("/_probe/raise")

    assert response.status_code == expected_status
    body = response.json()
    assert body["code"] == expected_code
    assert body["correlation_id"]


def test_access_denied_handler_carries_no_object_payload() -> None:
    """NFR08: the ``ACCESS_DENIED`` envelope must not carry an object payload.

    ``AccessDeniedError`` is raised by the shared authz helper *without*
    ``details`` (the only production construction site), so the wire body is
    exactly ``{code, message, correlation_id}`` — no participant id, no
    tenant id, no serialized object.

    NOTE (observed, NOT fixed here — src/ is out of scope for this ticket):
    ``DomainError.to_payload`` serialises ``details`` for *every* code, so an
    ``ACCESS_DENIED`` that a caller *did* attach ``details`` to would leak it.
    Production never does; ``test_access_denied_details_would_leak_if_supplied``
    pins that hazard so it cannot regress unnoticed.
    """
    app = _create_app()

    @app.get("/_probe/denied")
    async def _denied() -> dict:  # pragma: no cover - never returns
        raise AccessDeniedError("Forbidden")

    with TestClient(app, raise_server_exceptions=False) as test_client:
        response = test_client.get("/_probe/denied")

    assert response.status_code == 403
    body = response.json()
    assert body["code"] == "ACCESS_DENIED"
    assert set(body) == {"code", "message", "correlation_id"}
    assert "details" not in body
    assert "field_path" not in body


def test_access_denied_details_would_leak_if_supplied() -> None:
    """Hazard pin: ``to_payload`` is code-agnostic, so ``details`` on an
    ``ACCESS_DENIED`` reaches the client verbatim. Production must therefore
    never attach details to this code (see the note above)."""
    err = AccessDeniedError(
        "Forbidden", details={"participant_id": "8f2c-1234", "tenant_id": "9999"}
    )
    payload = err.to_payload()

    assert payload["code"] == "ACCESS_DENIED"
    assert payload["details"] == {"participant_id": "8f2c-1234", "tenant_id": "9999"}


def test_state_conflict_handler_does_pass_through_details() -> None:
    """Counter-test: ``details`` IS serialised for codes that opt in — this
    pins the envelope as a general pass-through so the ACCESS_DENIED
    assertion above stays meaningful."""
    app = _create_app()

    @app.get("/_probe/conflict")
    async def _conflict() -> dict:  # pragma: no cover - never returns
        raise StateConflictError("cannot transition", details={"from": "draft"})

    with TestClient(app, raise_server_exceptions=False) as test_client:
        response = test_client.get("/_probe/conflict")

    assert response.status_code == 409
    assert response.json()["details"] == {"from": "draft"}


# ---------------------------------------------------------------------------
# App wiring
# ---------------------------------------------------------------------------


def test_correlation_id_middleware_is_always_installed(client: TestClient) -> None:
    """Every response carries a correlation id, tracing on or off."""
    response = client.post("/_probe/one-required", json={})
    assert response.status_code == 422
    assert response.json()["correlation_id"]


def test_handler_registry_exposes_both_domain_handlers() -> None:
    """Both global handlers are registered on the app."""
    app: FastAPI = _create_app()
    registered = {
        exc.__name__ for exc in app.exception_handlers  # type: ignore[attr-defined]
    }
    assert "DomainError" in registered
    assert "RequestValidationError" in registered
    assert DomainError is not None