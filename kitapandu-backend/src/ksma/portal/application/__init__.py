"""Portal application services — ServiceRequest, InternalNotification, CommunicationPreference.

Minimal implementations that demonstrate the API surface. Notifications
subscribe to outbox events at app startup to build the in-app inbox.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from ksma.shared.errors import ValidationError
from ksma.shared.events import bus
from ksma.shared.outbox import enqueue
from ksma.shared.tenancy import TenantContext
from ksma.shared.time import clock


async def submit_service_request(
    session: AsyncSession,
    ctx: TenantContext,
    *,
    participant_id: UUID,
    category: str,
    description: str,
    source_ref: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if not description:
        raise ValidationError("description is required")
    if category not in {"academic", "schedule", "identity", "finance"}:
        raise ValidationError("category must be one of: academic, schedule, identity, finance")
    r = (
        await session.execute(
            text(
                """
                INSERT INTO service_request
                  (id, tenant_id, requester_account_id, participant_id, category,
                   source_ref, description, status,
                   created_at, created_by, updated_at, updated_by)
                VALUES
                  (ksma_uuid(), :tid, :sub, :pid, :cat,
                   CAST(:src AS JSONB), :desc, 'open',
                   now(), :cb, now(), :cb)
                RETURNING id, status
                """
            ),
            {
                "tid": str(ctx.tenant_id),
                "sub": str(ctx.account_id),
                "pid": str(participant_id),
                "cat": category,
                "src": _json(source_ref) if source_ref else None,
                "desc": description,
                "cb": str(ctx.account_id),
            },
        )
    ).first()
    await enqueue(
        session,
        __import__("ksma.portal.application", fromlist=[""])._REQ_SUBMITTED
        if False
        else __import__("ksma.shared.events", fromlist=["DomainEvent"]).DomainEvent(
            event_type="ServiceRequestSubmitted",
            tenant_id=ctx.tenant_id,
            actor_id=ctx.account_id,
        ),
        aggregate_ref=f"service_request:{r.id}",
    )
    bus().dispatch(
        __import__("ksma.shared.events", fromlist=["DomainEvent"]).DomainEvent(
            event_type="ServiceRequestSubmitted",
            tenant_id=ctx.tenant_id,
            actor_id=ctx.account_id,
        )
    )
    return {"id": r.id, "status": r.status}


async def resolve_service_request(
    session: AsyncSession,
    ctx: TenantContext,
    *,
    request_id: UUID,
    decision: str,
    resolution: str,
) -> None:
    if decision not in {"approved", "rejected", "closed"}:
        raise ValidationError("decision must be approved, rejected, or closed")
    await session.execute(
        text(
            """
            UPDATE service_request
            SET status = :d,
                resolution = CAST(:r AS JSONB),
                updated_at = now(),
                updated_by = :u
            WHERE id = :id AND tenant_id = :tid
            """
        ),
        {
            "d": decision,
            "r": _json({"actor": str(ctx.account_id), "resolution": resolution}),
            "u": str(ctx.account_id),
            "id": str(request_id),
            "tid": str(ctx.tenant_id),
        },
    )


async def list_inbox(
    session: AsyncSession,
    ctx: TenantContext,
    *,
    limit: int = 50,
) -> list[dict[str, Any]]:
    rows = (
        await session.execute(
            text(
                """
                SELECT id, source_ref, event_id, notification_kind, summary,
                       delivery_state, read_at, occurred_at
                FROM internal_notification
                WHERE tenant_id = :tid AND recipient_account_id = :sub
                ORDER BY occurred_at DESC
                LIMIT :lim
                """
            ),
            {"tid": str(ctx.tenant_id), "sub": str(ctx.account_id), "lim": limit},
        )
    ).all()
    return [
        {
            "id": str(r.id),
            "source_ref": r.source_ref,
            "event_id": str(r.event_id) if r.event_id else None,
            "notification_kind": r.notification_kind,
            "summary": r.summary,
            "delivery_state": r.delivery_state,
            "read_at": r.read_at,
            "occurred_at": r.occurred_at,
        }
        for r in rows
    ]


def _json(d: Any) -> str:
    import json

    return json.dumps(d, default=str)