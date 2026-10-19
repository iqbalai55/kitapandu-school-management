"""KSMA FastAPI application factory.

Mounts every bounded-context router under ``/api/v1`` and configures global
exception handlers. Routers that previously had a ``require_tenant_context``
placeholder (``raise NotImplementedError``) get the real dependency from
``shared.auth.dependencies``.

Run locally with::

    uvicorn ksma.main:app --reload --port 8000

OpenAPI: http://localhost:8000/docs
"""
from __future__ import annotations

import logging
from uuid import uuid4

from fastapi import FastAPI, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from ksma.assessment.interface import router as assessment_router
from ksma.batch.interface import router as batch_router
from ksma.finance.interface import router as finance_router
from ksma.identity.interface import router as identity_router
from ksma.operations.interface import router as operations_router
from ksma.participant.interface import router as participant_router
from ksma.portal.interface import router as portal_router
from ksma.program.interface import router as program_router
from ksma.session.interface import router as session_router
from ksma.settings import get_settings
from ksma.shared.errors import DomainError
from ksma.shared.events import DomainEvent, bus


logger = logging.getLogger("ksma.main")


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(
        title="KitaPandu School Management API",
        version="0.1.0",
        description="Pilot scope — Vaneta Maju Berdaya. See /docs/adr/0001-architecture-foundations.md.",
        docs_url="/docs",
        redoc_url="/redoc",
    )

    # --- Exception handlers -------------------------------------------------

    @app.exception_handler(DomainError)
    async def _domain_error_handler(request: Request, exc: DomainError) -> JSONResponse:
        # NFR08 + access matrix §3: never leak participant/object details.
        body = exc.to_payload()
        return JSONResponse(
            status_code=exc.http_status,
            content=body,
        )

    @app.exception_handler(RequestValidationError)
    async def _pydantic_error_handler(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        # Translate Pydantic validation errors into our error envelope.
        errors = exc.errors()
        first = errors[0] if errors else {}
        return JSONResponse(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            content={
                "code": "INVALID_VALUE",
                "message": first.get("msg", "Invalid request body"),
                "field_path": ".".join(str(p) for p in first.get("loc", ())),
                "correlation_id": str(uuid4()),
            },
        )

    # --- Routers ------------------------------------------------------------

    app.include_router(identity_router)
    app.include_router(participant_router)
    app.include_router(program_router)
    app.include_router(batch_router)
    app.include_router(session_router)
    app.include_router(finance_router)
    app.include_router(assessment_router)
    app.include_router(portal_router)
    app.include_router(operations_router)

    # --- Wire placeholder deps in every router that used NotImplementedError --
    #
    # Each context's ``interface/__init__.py`` defines a ``require_tenant_context``
    # placeholder that raises ``NotImplementedError``. We rebind it here to the
    # real dependency after import.

    from ksma.shared.auth.dependencies import tenant_context_dep

    for router_module in [
        "ksma.participant.interface",
        "ksma.program.interface",
        "ksma.batch.interface",
        "ksma.session.interface",
        "ksma.finance.interface",
        "ksma.assessment.interface",
        "ksma.operations.interface",
    ]:
        mod = __import__(router_module, fromlist=[""])
        if hasattr(mod, "require_tenant_context"):
            mod.require_tenant_context = tenant_context_dep  # type: ignore[attr-defined]

    # --- Health + root ------------------------------------------------------

    @app.get("/health", tags=["meta"])
    async def health() -> dict[str, str]:
        return {"status": "ok", "version": "0.1.0"}

    @app.get("/", tags=["meta"])
    async def root() -> dict[str, str]:
        return {
            "service": "KSMA",
            "version": "0.1.0",
            "docs": "/docs",
        }

    # --- Outbox event → InternalNotification subscriber (pilot stub) -------
    #
    # Subscribes to a few event types so portal/inbox has something to render.
    # Real production would queue to external channels per Q09.

    def _on_event_record_notification(event: DomainEvent) -> None:
        # The actual INSERT happens in the portal.application subscriber when
        # the outbox worker dispatches it (in the same DB transaction). This
        # in-process stub is left empty intentionally — it would only matter
        # if the same-tx dispatcher also wrote notifications.
        return

    for evt_type in (
        "InvoiceIssued",
        "PaymentVerified",
        "FundsAllocated",
        "SessionScheduled",
        "ReportPublished",
    ):
        bus().subscribe(DomainEvent, _on_event_record_notification)  # type: ignore[arg-type]

    return app


app = create_app()