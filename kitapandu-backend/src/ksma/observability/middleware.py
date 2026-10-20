"""Correlation-id middleware.

Always installed by :func:`ksma.main.create_app` regardless of whether OTel is
enabled, so the contract is the same in dev, CI, and prod:

- A correlation id is either taken from the inbound ``X-Correlation-Id`` request
  header (when present) or generated as a UUID4.
- The id is stored on :attr:`request.state.correlation_id`.
- The id is echoed back to the caller on the response under the same header.
- When OTel is enabled, the id is also attached as a span attribute on the
  current server span (so it shows up in traces).

This is the lifecycle seam with the rest of the codebase: downstream handlers
and audit loggers should ``correlation_id = request.state.correlation_id``
and pass it through (see ``ksma.shared.audit``).
"""
from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from uuid import uuid4

from fastapi import Request, Response
from starlette.middleware.base import BaseHTTPMiddleware

from ksma.observability import _is_enabled, inject_correlation_id

logger = logging.getLogger("ksma.observability.middleware")


CORRELATION_ID_HEADER = "X-Correlation-Id"


class CorrelationIdMiddleware(BaseHTTPMiddleware):
    """Starlette middleware that propagates ``X-Correlation-Id`` end-to-end.

    Add it via ``app.add_middleware(CorrelationIdMiddleware)`` — it must be
    added *before* any middleware that wants to read ``request.state``.
    """

    async def dispatch(
        self,
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        inbound = request.headers.get(CORRELATION_ID_HEADER)
        # Treat empty strings the same as missing — fall back to a fresh UUID.
        correlation_id = inbound.strip() if inbound and inbound.strip() else str(uuid4())

        request.state.correlation_id = correlation_id

        if _is_enabled():
            # Set on the active span (no-op if none is active yet).
            inject_correlation_id(correlation_id)

        response = await call_next(request)
        response.headers[CORRELATION_ID_HEADER] = correlation_id
        return response


__all__ = ["CORRELATION_ID_HEADER", "CorrelationIdMiddleware"]
