"""KSMA observability — OpenTelemetry SDK wiring (P4.1).

Design contract
---------------
The whole module is **opt-in** and gated by ``settings.otel_enabled``. When that
flag is ``False`` (the default), the public API surface in this module is a
**no-op** — no global tracer provider is installed, no spans are emitted, no
instrumentation is registered, and ``get_tracer()`` returns a tiny ``NoOpTracer``
proxy. There must be no measurable import-time or per-request overhead in this
mode (NFR-perf of the pilot, and the operational "off by default" guarantee in
issue #31).

When ``otel_enabled=True``, ``setup_tracing()`` initialises exactly one
``TracerProvider`` per process, configures the OTLP gRPC exporter from
``settings.otel_otlp_endpoint``, instruments FastAPI and SQLAlchemy via the
official OTel contrib packages, and may later be extended with auto- or
manual-instrumentation for other libraries.

The correlation-id middleware (``ksma.observability.middleware``) is always
installed: it sets ``request.state.correlation_id`` and echoes the header even
when OTel is disabled, so upstream gateways and downstream services can rely
on the contract regardless of whether tracing is on.

Public API
----------
- :func:`setup_tracing` — initialise the SDK + instrumentation (idempotent).
- :func:`get_tracer` — get a tracer; safe when OTel is disabled.
- :func:`inject_correlation_id` — set the ``correlation_id`` attribute on the
  current span (if any).

Configuration
-------------
All knobs are in :class:`ksma.settings.Settings` (``otel_enabled``,
``otel_service_name``, ``otel_otlp_endpoint``, ``otel_sample_ratio``) and
mapped to env vars prefixed with ``KITAPANDU_`` (e.g.
``KITAPANDU_OTEL_ENABLED=true``).
"""
from __future__ import annotations

import logging
import threading
from contextlib import nullcontext
from typing import Any

from fastapi import FastAPI
from sqlalchemy.ext.asyncio import AsyncEngine

from ksma.settings import get_settings

logger = logging.getLogger("ksma.observability")


# ---------------------------------------------------------------------------
# No-op tracer — used when OTel is disabled.
# ---------------------------------------------------------------------------
#
# We deliberately do NOT use ``opentelemetry.trace.NoOpTracer``. Reason: the
# spec forbids installing a real ``TracerProvider`` when the flag is off, and
# we still want a typed return so call sites don't have to branch. This is a
# tiny duck-typed proxy covering the small subset of Tracer methods we use in
# the pilot: ``start_as_current_span`` (context manager) and nothing else.

class _NoOpSpan:
    """Minimal stand-in for ``opentelemetry.trace.Span`` when OTel is off."""

    def set_attribute(self, key: str, value: Any) -> None:
        return None

    def set_status(self, *args: Any, **kwargs: Any) -> None:
        return None

    def record_exception(self, *args: Any, **kwargs: Any) -> None:
        return None

    def end(self) -> None:
        return None


class _NoOpTracer:
    """Stand-in for ``opentelemetry.trace.Tracer`` when OTel is off.

    Public-facing methods used in the pilot all return ``_NoOpSpan``-shaped
    objects. This is safe for ``with tracer.start_as_current_span(...)``
    usage and for ``span.set_attribute(...)`` calls guarded by an enabled-flag
    check.
    """

    def start_as_current_span(self, name: str, *args: Any, **kwargs: Any):
        # Return a context manager that yields a no-op span.
        return nullcontext(_NoOpSpan())

    def start_span(self, name: str, *args: Any, **kwargs: Any) -> _NoOpSpan:
        return _NoOpSpan()


# ---------------------------------------------------------------------------
# Module-level state. The flag is read from settings on first use and cached
# so we don't keep re-parsing env vars on the hot path.
# ---------------------------------------------------------------------------

_setup_lock = threading.Lock()
_setup_done: bool = False
_enabled_cached: bool | None = None


def _is_enabled() -> bool:
    """Read the ``otel_enabled`` flag once and cache it.

    Settings are themselves lru_cached by :func:`ksma.settings.get_settings`,
    so this is cheap even without our local cache. The local cache exists so
    that tests can monkey-patch the observable flag (see
    ``tests/unit/test_observability.py``).
    """
    global _enabled_cached  # noqa: PLW0603 — intentional module-level cache
    if _enabled_cached is None:
        _enabled_cached = bool(get_settings().otel_enabled)
    return _enabled_cached


def _reset_for_test() -> None:
    """Test helper — drop the cached enable flag so a re-read picks up a fresh
    setting (e.g. an env var change mid-test)."""
    global _enabled_cached, _setup_done  # noqa: PLW0603 — intentional test seam
    _enabled_cached = None
    _setup_done = False


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def setup_tracing(app: FastAPI, engine: AsyncEngine) -> None:
    """Initialise the OpenTelemetry SDK + auto-instrumentation.

    When ``settings.otel_enabled`` is ``False`` this is a strict no-op
    (no provider installed, no instrumentation touched, no imports of the
    contrib instrumentors' lazy hooks beyond what Python normally does).

    When ``settings.otel_enabled`` is ``True``:

    1. Install a single :class:`TracerProvider` configured with the service
       name and sampler from settings.
    2. Attach an OTLP/gRPC exporter pointed at
       ``settings.otel_otlp_endpoint`` (if set).
    3. Wire the FastAPI app + SQLAlchemy engine via the contrib
       instrumentors.
    4. The function is idempotent across multiple calls: a re-entrant call
       is logged and skipped, so it is safe to invoke from both ``create_app``
       and any lifespan startup hook in the future.
    """
    if not _is_enabled():
        logger.debug("OTel disabled (KITAPANDU_OTEL_ENABLED=false) — setup_tracing is a no-op")
        return

    global _setup_done  # noqa: PLW0603 — intentional module-level idempotency guard

    with _setup_lock:
        if _setup_done:
            logger.debug("OTel setup already done — skipping redundant setup_tracing call")
            return

        settings = get_settings()

        # Importing these only when enabled keeps the disabled path zero-cost
        # at import time (the contrib packages pull in sqlalchemy/pydantic/etc.).
        from opentelemetry import trace  # noqa: PLC0415 — lazy on purpose
        from opentelemetry.sdk.resources import SERVICE_NAME, Resource  # noqa: PLC0415
        from opentelemetry.sdk.trace import TracerProvider  # noqa: PLC0415
        from opentelemetry.sdk.trace.export import BatchSpanProcessor  # noqa: PLC0415

        from ksma.observability.instrumentation import (  # noqa: PLC0415 — lazy
            instrument_fastapi,
            instrument_sqlalchemy,
        )

        resource = Resource.create({SERVICE_NAME: settings.otel_service_name})
        provider = TracerProvider(
            resource=resource,
            sampler=_build_sampler(settings.otel_sample_ratio),
        )

        if settings.otel_otlp_endpoint:
            from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import (  # noqa: PLC0415 — lazy
                OTLPSpanExporter,
            )

            exporter = OTLPSpanExporter(endpoint=settings.otel_otlp_endpoint, insecure=True)
            provider.add_span_processor(BatchSpanProcessor(exporter))
            logger.info(
                "OTel OTLP exporter configured",
                extra={"endpoint": settings.otel_otlp_endpoint},
            )
        else:
            logger.info(
                "OTel enabled but no KITAPANDU_OTEL_EXPORTER_OTLP_ENDPOINT set — "
                "spans will be created but not exported"
            )

        trace.set_tracer_provider(provider)

        # Auto-instrumentation. Order matters slightly: FastAPI first so the
        # outer request span exists before SQLAlchemy child spans appear.
        instrument_fastapi(app)
        instrument_sqlalchemy(engine)

        _setup_done = True
        logger.info("OTel SDK initialised", extra={"service": settings.otel_service_name})


def _build_sampler(ratio: float):
    """Map a 0..1 sample-ratio onto a TraceIdRatioBased sampler (clamped)."""
    from opentelemetry.sdk.trace.sampling import TraceIdRatioBased  # noqa: PLC0415 — lazy

    if ratio <= 0.0:
        return TraceIdRatioBased(0.0)
    if ratio >= 1.0:
        return TraceIdRatioBased(1.0)
    return TraceIdRatioBased(ratio)


def get_tracer(name: str):
    """Return a tracer.

    When OTel is enabled, this delegates to
    ``opentelemetry.trace.get_tracer(name)``; otherwise returns a
    :class:`_NoOpTracer` instance. Call sites can use either uniformly::

        tracer = get_tracer(__name__)
        with tracer.start_as_current_span("do_thing"):
            ...

    """
    if not _is_enabled():
        return _NoOpTracer()

    from opentelemetry import trace  # noqa: PLC0415 — lazy on purpose

    return trace.get_tracer(name)


def inject_correlation_id(correlation_id: str) -> None:
    """Attach a ``correlation_id`` attribute to the current span.

    Safe to call when OTel is disabled (becomes a no-op). Safe to call when
    no span is currently active (also a no-op — the underlying
    :func:`trace.get_current_span` returns an ``INVALID`` non-recording span
    in that case).

    We try/except to make this truly defensive: any AttributeError / value
    shape weirdness must not break the request path. The correlation id
    itself stays on ``request.state.correlation_id`` (see middleware) as the
    source of truth.
    """
    if not _is_enabled():
        return

    try:
        from opentelemetry import trace  # noqa: PLC0415 — lazy on purpose

        span = trace.get_current_span()
        if span is None:
            return
        # NonRecordingSpan / INVALID spans silently swallow set_attribute.
        span.set_attribute("correlation_id", correlation_id)
    except Exception:
        logger.debug("inject_correlation_id silently ignored an error", exc_info=True)


__all__ = [
    # Test helper — not part of the public contract but exported for tests
    # that need to flip the cached enabled flag.
    "_reset_for_test",
    # Public API.
    "get_tracer",
    "inject_correlation_id",
    "setup_tracing",
]
