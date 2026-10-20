"""Unit tests for the OpenTelemetry wiring in :mod:`ksma.observability`.

Scope
-----
The P4.1 ticket is explicit that ``KITAPANDU_OTEL_ENABLED=false`` (the default)
must be a strict no-op end to end. These tests pin that contract:

- ``setup_tracing`` is a no-op when OTel is disabled — no global tracer
  provider is installed.
- ``get_tracer`` returns a NoOp-like object that is callable as a context
  manager (``with tracer.start_as_current_span("x")``).
- ``inject_correlation_id`` does not raise regardless of OTel state.
- The correlation-id middleware reads the inbound ``X-Correlation-Id``
  header (when present) or generates a fresh UUID4 (when absent), and echoes
  the value back on the response.

We use :class:`httpx.AsyncClient` with :class:`ASGITransport` to exercise the
middleware end-to-end against a tiny standalone FastAPI app — we don't depend
on the full app factory because we'd need the DB and JWT stack to import
``ksma.main``.
"""
from __future__ import annotations

import os
from collections.abc import Iterator
from uuid import UUID

import pytest
from fastapi import FastAPI, Request
from httpx import ASGITransport, AsyncClient
from opentelemetry import trace as otel_trace

from ksma.observability import (
    _is_enabled,
    _reset_for_test,
    get_tracer,
    inject_correlation_id,
    setup_tracing,
)
from ksma.observability.middleware import CORRELATION_ID_HEADER, CorrelationIdMiddleware
from ksma.settings import get_settings as _gs

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _ensure_otel_disabled() -> Iterator[None]:
    """Pin OTel to disabled for every test in this module.

    We don't want a stray ``KITAPANDU_OTEL_ENABLED=true`` in the developer's
    shell to silently change behaviour. ``_reset_for_test`` clears the module
    cache so each test starts from a known state.

    Also seeds the *required* settings fields (postgres_dsn, jwt_secret,
    minio keys) so ``Settings()`` validates in isolation from the rest of the
    test suite. The actual values are not used by anything exercised here.
    """
    prev_otel = os.environ.get("KITAPANDU_OTEL_ENABLED")
    prev_required = {
        k: os.environ.get(k) for k in (
            "KITAPANDU_POSTGRES_DSN",
            "KITAPANDU_POSTGRES_DSN_SYNC",
            "KITAPANDU_JWT_SECRET",
            "KITAPANDU_MINIO_ACCESS_KEY",
            "KITAPANDU_MINIO_SECRET_KEY",
        )
    }
    os.environ["KITAPANDU_OTEL_ENABLED"] = "false"
    os.environ["KITAPANDU_POSTGRES_DSN"] = "postgresql+asyncpg://x:x@localhost/x"
    os.environ["KITAPANDU_POSTGRES_DSN_SYNC"] = "postgresql://x:x@localhost/x"
    os.environ["KITAPANDU_JWT_SECRET"] = "test-secret-not-used"
    os.environ["KITAPANDU_MINIO_ACCESS_KEY"] = "test-access-key"
    os.environ["KITAPANDU_MINIO_SECRET_KEY"] = "test-secret-key"

    # Settings is lru_cached — drop the cache so the seeded env vars take effect.
    _gs.cache_clear()

    try:
        _reset_for_test()
        # Sanity check — guard against regressions where defaults flip.
        assert _is_enabled() is False
        yield
    finally:
        for k, v in prev_required.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        if prev_otel is None:
            os.environ.pop("KITAPANDU_OTEL_ENABLED", None)
        else:
            os.environ["KITAPANDU_OTEL_ENABLED"] = prev_otel
        _gs.cache_clear()
        _reset_for_test()


@pytest.fixture
def tiny_app() -> FastAPI:
    """A bare FastAPI app with the correlation middleware + a sentinel endpoint.

    Keeps the test surface narrow — we don't want the full KSMA app to need
    a DB to be importable here.
    """
    app = FastAPI()
    app.add_middleware(CorrelationIdMiddleware)

    captured: dict[str, str] = {}

    @app.get("/echo")
    async def echo(request: Request) -> dict[str, str]:
        # Expose the state we care about so assertions can see it.
        captured["correlation_id"] = getattr(request.state, "correlation_id", "")
        return {"correlation_id": captured["correlation_id"]}

    # Stash `captured` on the app for tests that want a fresh capture per call.
    app.state.captured = captured  # type: ignore[attr-defined]
    return app


# ---------------------------------------------------------------------------
# Disabled-by-default contract
# ---------------------------------------------------------------------------


class TestSetupTracingNoOpWhenDisabled:
    def test_setup_tracing_is_noop_when_disabled(self) -> None:
        """setup_tracing with otel_enabled=False must not install a tracer
        provider and must not raise."""
        # Snapshot the current tracer provider so we can detect mutation.
        before = otel_trace.get_tracer_provider()
        # Swallow any type so tests don't depend on the exact default class.
        before_type = type(before)

        # Use a stub engine — setup_tracing must NEVER touch the engine
        # when disabled.
        sentinel = object()

        # Should be a no-op (no exception, no work).
        setup_tracing(FastAPI(), sentinel)  # type: ignore[arg-type]

        # Confirm the global tracer provider is the same object as before
        # — trace.set_tracer_provider must NOT have been called.
        after = otel_trace.get_tracer_provider()
        assert after is before
        assert type(after) is before_type


class TestGetTracerWhenDisabled:
    def test_returns_a_callable_object(self) -> None:
        tracer = get_tracer("ksma.tests")
        assert tracer is not None
        # Must respond to the small API we use in the pilot.
        assert hasattr(tracer, "start_as_current_span")

    def test_start_as_current_span_works_as_context_manager(self) -> None:
        tracer = get_tracer("ksma.tests")
        with tracer.start_as_current_span("unit-test-span") as span:
            # Must expose set_attribute without raising.
            span.set_attribute("ping", "pong")
            assert span is not None


class TestInjectCorrelationIdWhenDisabled:
    def test_does_not_raise(self) -> None:
        # No exception = pass. We deliberately don't check a side effect —
        # there shouldn't be one when OTel is off.
        inject_correlation_id("corr-test-123")

    def test_does_not_raise_with_arbitrary_string(self) -> None:
        inject_correlation_id("not-a-uuid-but-we-do-not-care")


# ---------------------------------------------------------------------------
# Middleware behaviour (independent of OTel state — always on)
# ---------------------------------------------------------------------------


class TestCorrelationIdMiddleware:
    @pytest.mark.asyncio
    async def test_generates_uuid_when_header_absent(self, tiny_app: FastAPI) -> None:
        async with AsyncClient(
            transport=ASGITransport(app=tiny_app), base_url="http://test"
        ) as client:
            resp = await client.get("/echo")

        assert resp.status_code == 200
        cid = resp.headers[CORRELATION_ID_HEADER]
        # Must be a valid UUID4 (length + dashes + hex).
        parsed = UUID(cid)
        assert str(parsed) == cid

        # Same id should have been visible to the handler via request.state.
        body = resp.json()
        assert body["correlation_id"] == cid

    @pytest.mark.asyncio
    async def test_uses_inbound_header_when_present(self, tiny_app: FastAPI) -> None:
        inbound = "client-supplied-corr-id-abcdef"

        async with AsyncClient(
            transport=ASGITransport(app=tiny_app), base_url="http://test"
        ) as client:
            resp = await client.get("/echo", headers={CORRELATION_ID_HEADER: inbound})

        assert resp.status_code == 200
        assert resp.headers[CORRELATION_ID_HEADER] == inbound
        assert resp.json()["correlation_id"] == inbound

    @pytest.mark.asyncio
    async def test_treats_empty_header_as_absent(self, tiny_app: FastAPI) -> None:
        """Some clients send ``X-Correlation-Id:`` (empty). We must generate a
        fresh UUID rather than echo an empty string."""
        async with AsyncClient(
            transport=ASGITransport(app=tiny_app), base_url="http://test"
        ) as client:
            resp = await client.get("/echo", headers={CORRELATION_ID_HEADER: ""})

        assert resp.status_code == 200
        cid = resp.headers[CORRELATION_ID_HEADER]
        # Generated UUID, not empty.
        assert cid
        UUID(cid)

    @pytest.mark.asyncio
    async def test_each_request_gets_independent_id(self, tiny_app: FastAPI) -> None:
        async with AsyncClient(
            transport=ASGITransport(app=tiny_app), base_url="http://test"
        ) as client:
            resp_a = await client.get("/echo")
            resp_b = await client.get("/echo")

        assert resp_a.headers[CORRELATION_ID_HEADER] != resp_b.headers[CORRELATION_ID_HEADER]
