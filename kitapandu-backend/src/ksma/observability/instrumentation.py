"""Auto-instrumentation wrappers for FastAPI + SQLAlchemy.

Both helpers are strict no-ops when :data:`OTEL_ENABLED` is False. They are
isolated in their own module so the contrib-package imports are not paid for
in the disabled-by-default path.
"""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from fastapi import FastAPI
from sqlalchemy.ext.asyncio import AsyncEngine

from ksma.observability import _is_enabled

if TYPE_CHECKING:
    pass  # nothing extra to type-check

logger = logging.getLogger("ksma.observability.instrumentation")


def instrument_fastapi(app: FastAPI) -> None:
    """Instrument a FastAPI app. No-op when OTel is disabled.

    Uses :func:`FastAPIInstrumentor.instrument_app` so we target a single
    app instance (not the global ``fastapi`` instrumentation), which is the
    safe multi-app pattern.
    """
    if not _is_enabled():
        return

    from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor  # noqa: PLC0415

    FastAPIInstrumentor.instrument_app(app)
    logger.debug("FastAPI instrumentation enabled")


def instrument_sqlalchemy(engine: AsyncEngine) -> None:
    """Instrument an SQLAlchemy :class:`AsyncEngine`. No-op when disabled.

    The contrib instrumentor walks the underlying sync engine (``engine.sync_engine``).
    SQLAlchemy 2.x exposes this attribute — for an ``AsyncEngine`` it points at
    the wrapped ``Engine`` the async machinery delegates to. Re-instrumenting
    the same engine twice raises, so we wrap the call defensively.
    """
    if not _is_enabled():
        return

    from opentelemetry.instrumentation.sqlalchemy import SQLAlchemyInstrumentor  # noqa: PLC0415

    sync_engine = getattr(engine, "sync_engine", None)
    if sync_engine is None:
        logger.warning(
            "AsyncEngine has no .sync_engine attribute — skipping SQLAlchemy instrumentation"
        )
        return

    try:
        SQLAlchemyInstrumentor().instrument(engine=sync_engine)
        logger.debug("SQLAlchemy instrumentation enabled")
    except Exception:
        logger.debug("SQLAlchemy engine already instrumented — skipping", exc_info=True)


__all__ = ["instrument_fastapi", "instrument_sqlalchemy"]
