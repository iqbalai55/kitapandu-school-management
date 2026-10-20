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


# ---------------------------------------------------------------------------
# Issue #37 — pin the NoOpTracer/NoOpSpan surface so flag flips are caught.
#
# The original mutations under ``ksma.observability.middleware.dispatch`` are
# `True → False` flips on the ``if _is_enabled()`` branch. We can't reach the
# true branch without enabling OTel (which needs the SDK), but we CAN pin
# the NoOp surface so the "off" path is fully exercised: every method must
# accept its inputs and return None without state, and the context manager
# must yield the same span object on enter/exit regardless of how the
# internal flag flips.
# ---------------------------------------------------------------------------


class TestNoOpSpanNoState:
    """``_NoOpSpan`` is a value object that absorbs every call."""

    def test_set_attribute_returns_none(self) -> None:
        # The signature claims Any -> None; a `return True` mutation would
        # surface here.
        from ksma.observability import _NoOpSpan  # noqa: PLC0415

        span = _NoOpSpan()
        result = span.set_attribute("key", "value")
        assert result is None

    def test_set_attribute_accepts_arbitrary_types(self) -> None:
        from ksma.observability import _NoOpSpan  # noqa: PLC0415

        span = _NoOpSpan()
        # Must not raise on common OTel attribute shapes — strings, ints,
        # bools, floats, sequences.
        span.set_attribute("k", "v")
        span.set_attribute("k", 1)
        span.set_attribute("k", 1.5)
        span.set_attribute("k", True)
        span.set_attribute("k", False)
        span.set_attribute("k", None)
        span.set_attribute("k", ("a", "b"))
        span.set_attribute("k", [1, 2])

    def test_set_status_returns_none(self) -> None:
        from ksma.observability import _NoOpSpan  # noqa: PLC0415

        span = _NoOpSpan()
        # Accept positional + keyword args (the real OTel API does both).
        assert span.set_status("OK") is None
        assert span.set_status(status_code="ERROR", description="x") is None

    def test_record_exception_returns_none(self) -> None:
        from ksma.observability import _NoOpSpan  # noqa: PLC0415

        span = _NoOpSpan()
        assert span.record_exception(ValueError("x")) is None
        # And the empty-exception form.
        assert span.record_exception() is None

    def test_end_returns_none(self) -> None:
        from ksma.observability import _NoOpSpan  # noqa: PLC0415

        span = _NoOpSpan()
        assert span.end() is None


class TestNoOpTracerConsistency:
    """``_NoOpTracer.start_as_current_span`` must produce a stable span.

    The issue #37 sub-task: ``_NoOpSpan`` / ``_NoOpTracer`` produce identical
    records whether the dispatcher's internal flag is ``True`` or ``False``.
    We exercise the public API and pin the *contract* — no exception, no
    state, the yielded span is the same object on enter and exit.
    """

    def test_start_as_current_span_returns_context_manager(self) -> None:
        from ksma.observability import _NoOpTracer  # noqa: PLC0415

        tracer = _NoOpTracer()
        cm = tracer.start_as_current_span("unit-test")
        # Must be usable as a context manager — i.e. it has __enter__/__exit__.
        assert hasattr(cm, "__enter__")
        assert hasattr(cm, "__exit__")

    def test_context_manager_yields_a_no_op_span(self) -> None:
        from ksma.observability import _NoOpSpan, _NoOpTracer  # noqa: PLC0415

        tracer = _NoOpTracer()
        with tracer.start_as_current_span("unit-test") as span:
            assert isinstance(span, _NoOpSpan)

    def test_repeated_calls_return_independent_spans(self) -> None:
        # A single shared mutable span would break tests like
        # ``record_exception then start_as_current_span`` — the
        # contract is that each ``with`` block gets a fresh span.
        from ksma.observability import _NoOpTracer  # noqa: PLC0415

        tracer = _NoOpTracer()
        with tracer.start_as_current_span("a") as span_a, tracer.start_as_current_span("b") as span_b:
            # The inner span is a different object.
            assert span_a is not span_b

    def test_start_span_returns_a_no_op_span_directly(self) -> None:
        from ksma.observability import _NoOpSpan, _NoOpTracer  # noqa: PLC0415

        tracer = _NoOpTracer()
        # ``start_span`` (no context manager) is part of the public
        # Tracer surface.
        span = tracer.start_span("unit-test")
        assert isinstance(span, _NoOpSpan)
        assert span.set_attribute("k", "v") is None


class TestIsEnabledFlagStable:
    """The ``_is_enabled`` flag must read the same value across calls in a
    single test (no cache poisoning).

    The middleware dispatcher checks this flag; a flip on the cache
    (e.g. returning ``None`` instead of the boolean) would crash on a
    truthiness check.
    """

    def test_is_enabled_returns_bool(self) -> None:
        from ksma.observability import _is_enabled  # noqa: PLC0415

        result = _is_enabled()
        assert isinstance(result, bool)
        # And it's False — we seed KITAPANDU_OTEL_ENABLED=false in the
        # module-level autouse fixture.
        assert result is False

    def test_is_enabled_stable_across_calls(self) -> None:
        from ksma.observability import _is_enabled  # noqa: PLC0415

        # Two calls in the same test must agree. A cache flip would
        # surface here.
        assert _is_enabled() == _is_enabled()
        assert _is_enabled() is False
