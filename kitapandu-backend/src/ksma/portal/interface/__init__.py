"""Portal FastAPI router."""
from __future__ import annotations

from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, Query, status
from pydantic import BaseModel, Field

from ksma.portal.application import list_inbox, resolve_service_request, submit_service_request
from ksma.shared.auth.dependencies import require_action, tenant_context_dep
from ksma.shared.persistence import session_scope
from ksma.shared.rbac import ActionCode
from ksma.shared.tenancy import TenantContext

router = APIRouter(prefix="/api/v1", tags=["portal"])


class ServiceRequestBody(BaseModel):
    participant_id: UUID
    category: str = Field(pattern="^(academic|schedule|identity|finance)$")
    description: str = Field(min_length=1)
    source_ref: dict[str, Any] | None = None


class ResolveBody(BaseModel):
    decision: str = Field(pattern="^(approved|rejected|closed)$")
    resolution: str = Field(min_length=1, max_length=1000)


@router.post("/portal/requests", status_code=status.HTTP_201_CREATED)
async def post_request(
    body: ServiceRequestBody,
    ctx: TenantContext = Depends(require_action(ActionCode.PRT_A05)),
) -> dict[str, Any]:
    async with session_scope() as session:
        return await submit_service_request(
            session,
            ctx,
            participant_id=body.participant_id,
            category=body.category,
            description=body.description,
            source_ref=body.source_ref,
        )


@router.post("/portal/requests/{request_id}/resolve")
async def post_resolve(
    request_id: UUID,
    body: ResolveBody,
    ctx: TenantContext = Depends(require_action(ActionCode.PRT_A06)),
) -> None:
    async with session_scope() as session:
        await resolve_service_request(
            session,
            ctx,
            request_id=request_id,
            decision=body.decision,
            resolution=body.resolution,
        )


@router.get("/portal/inbox")
async def get_inbox(
    limit: int = Query(default=50, ge=1, le=200),
    ctx: TenantContext = Depends(tenant_context_dep),
) -> list[dict[str, Any]]:
    async with session_scope() as session:
        return await list_inbox(session, ctx, limit=limit)