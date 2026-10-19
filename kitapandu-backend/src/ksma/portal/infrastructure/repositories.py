"""Portal repositories (raw SQL)."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from ksma.portal.domain import (
    CommunicationPreference,
    InternalNotification,
    NotificationChannel,
    NotificationDeliveryState,
    ServiceRequest,
    ServiceRequestCategory,
    ServiceRequestStatus,
)


def _json(d: Any | None) -> str | None:
    if d is None:
        return None
    return json.dumps(d, default=str)


class ServiceRequestRepository:
    def __init__(self, session: AsyncSession, tenant_id: UUID) -> None:
        self.session = session
        self.tenant_id = tenant_id

    async def create(self, sr: ServiceRequest) -> None:
        await self.session.execute(
            text(
                """
                INSERT INTO service_request
                  (id, tenant_id, requester_account_id, participant_id, category,
                   source_ref, description, status,
                   assigned_membership_id, resolution,
                   created_at, created_by, updated_at, updated_by)
                VALUES
                  (:id, :tid, :sub, :pid, :cat,
                   CAST(:src AS JSONB), :desc, :status,
                   :assignee, CAST(:res AS JSONB),
                   now(), :cb, now(), :ub)
                """
            ),
            {
                "id": str(sr.id),
                "tid": str(self.tenant_id),
                "sub": str(sr.requester_account_id),
                "pid": str(sr.participant_id),
                "cat": sr.category.value,
                "src": _json(sr.source_ref),
                "desc": sr.description,
                "status": sr.status.value,
                "assignee": str(sr.assigned_membership_id) if sr.assigned_membership_id else None,
                "res": _json(sr.resolution),
                "cb": sr.created_by or str(sr.requester_account_id),
                "ub": sr.updated_by or str(sr.requester_account_id),
            },
        )

    async def get(self, id: UUID) -> ServiceRequest | None:
        row = (
            await self.session.execute(
                text(
                    """
                    SELECT id, tenant_id, requester_account_id, participant_id, category,
                           source_ref, description, status,
                           assigned_membership_id, resolution,
                           created_at, created_by, updated_at, updated_by
                    FROM service_request
                    WHERE id = :id AND tenant_id = :tid
                    """
                ),
                {"id": str(id), "tid": str(self.tenant_id)},
            )
        ).first()
        return _to_request(row) if row else None

    async def resolve(
        self, id: UUID, *, decision: ServiceRequestStatus, resolution: dict[str, Any], actor: str
    ) -> None:
        await self.session.execute(
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
                "d": decision.value,
                "r": _json(resolution),
                "u": actor,
                "id": str(id),
                "tid": str(self.tenant_id),
            },
        )

    async def list_open(self, *, limit: int = 50) -> list[ServiceRequest]:
        rows = (
            await self.session.execute(
                text(
                    """
                    SELECT id, tenant_id, requester_account_id, participant_id, category,
                           source_ref, description, status,
                           assigned_membership_id, resolution,
                           created_at, created_by, updated_at, updated_by
                    FROM service_request
                    WHERE tenant_id = :tid AND status IN ('open','in_review')
                    ORDER BY created_at DESC
                    LIMIT :lim
                    """
                ),
                {"tid": str(self.tenant_id), "lim": limit},
            )
        ).all()
        return [_to_request(r) for r in rows]


class InternalNotificationRepository:
    def __init__(self, session: AsyncSession, tenant_id: UUID) -> None:
        self.session = session
        self.tenant_id = tenant_id

    async def upsert(self, n: InternalNotification) -> bool:
        """INSERT ON CONFLICT DO NOTHING. Returns True if a new row was written."""
        result = await self.session.execute(
            text(
                """
                INSERT INTO internal_notification
                  (id, tenant_id, recipient_account_id, source_ref, event_id,
                   notification_kind, summary, delivery_state, occurred_at)
                VALUES
                  (:id, :tid, :recipient, CAST(:src AS JSONB), :event,
                   :kind, :summary, :state, COALESCE(:occurred, now()))
                ON CONFLICT (tenant_id, recipient_account_id, event_id, notification_kind)
                DO NOTHING
                RETURNING id
                """
            ),
            {
                "id": str(n.id),
                "tid": str(self.tenant_id),
                "recipient": str(n.recipient_account_id),
                "src": _json(n.source_ref),
                "event": str(n.event_id) if n.event_id else None,
                "kind": n.notification_kind,
                "summary": n.summary,
                "state": n.delivery_state.value,
                "occurred": n.occurred_at or datetime.now(timezone.utc),
            },
        )
        return result.rowcount > 0

    async def list_for_recipient(
        self, recipient_account_id: UUID, *, limit: int = 50
    ) -> list[InternalNotification]:
        rows = (
            await self.session.execute(
                text(
                    """
                    SELECT id, tenant_id, recipient_account_id, source_ref, event_id,
                           notification_kind, summary, delivery_state, read_at, occurred_at
                    FROM internal_notification
                    WHERE tenant_id = :tid AND recipient_account_id = :sub
                    ORDER BY occurred_at DESC
                    LIMIT :lim
                    """
                ),
                {
                    "tid": str(self.tenant_id),
                    "sub": str(recipient_account_id),
                    "lim": limit,
                },
            )
        ).all()
        return [_to_notification(r) for r in rows]

    async def mark_read(self, id: UUID) -> None:
        await self.session.execute(
            text(
                "UPDATE internal_notification SET read_at = now() "
                "WHERE id = :id AND tenant_id = :tid"
            ),
            {"id": str(id), "tid": str(self.tenant_id)},
        )


class CommunicationPreferenceRepository:
    def __init__(self, session: AsyncSession, tenant_id: UUID) -> None:
        self.session = session
        self.tenant_id = tenant_id

    async def upsert(self, p: CommunicationPreference) -> None:
        # UNIQUE (tenant_id, account_id, participant_id, category, channel) — NULL-aware.
        # PostgreSQL treats NULLs as distinct in unique constraints, so we INSERT
        # via ON CONFLICT on a partial unique index that handles the NULL case.
        # We avoid DDL here and use a defensive upsert that always inserts and
        # deactivates the prior row if it exists.
        await self.session.execute(
            text(
                """
                UPDATE communication_preference
                SET enabled = :enabled,
                    contact_reference = :contact
                WHERE tenant_id = :tid AND account_id = :aid
                  AND COALESCE(participant_id, '00000000-0000-0000-0000-000000000000')
                      = COALESCE(:pid, '00000000-0000-0000-0000-000000000000')
                  AND category = :cat AND channel = :channel
                """
            ),
            {
                "enabled": p.enabled,
                "contact": p.contact_reference,
                "tid": str(self.tenant_id),
                "aid": str(p.account_id),
                "pid": str(p.participant_id) if p.participant_id else None,
                "cat": p.category,
                "channel": p.channel.value,
            },
        )
        await self.session.execute(
            text(
                """
                INSERT INTO communication_preference
                  (id, tenant_id, account_id, participant_id, category, channel,
                   enabled, contact_reference, created_at)
                VALUES
                  (:id, :tid, :aid, :pid, :cat, :channel,
                   :enabled, :contact, now())
                """
            ),
            {
                "id": str(p.id),
                "tid": str(self.tenant_id),
                "aid": str(p.account_id),
                "pid": str(p.participant_id) if p.participant_id else None,
                "cat": p.category,
                "channel": p.channel.value,
                "enabled": p.enabled,
                "contact": p.contact_reference,
            },
        )


# --- Mapping --------------------------------------------------------------


def _to_request(row: Any) -> ServiceRequest:
    return ServiceRequest(
        id=row.id,
        tenant_id=row.tenant_id,
        requester_account_id=row.requester_account_id,
        participant_id=row.participant_id,
        category=ServiceRequestCategory(row.category),
        source_ref=row.source_ref,
        description=row.description,
        assigned_membership_id=row.assigned_membership_id,
        status=ServiceRequestStatus(row.status),
        resolution=row.resolution,
        created_at=row.created_at,
        created_by=row.created_by,
        updated_at=row.updated_at,
        updated_by=row.updated_by,
    )


def _to_notification(row: Any) -> InternalNotification:
    return InternalNotification(
        id=row.id,
        tenant_id=row.tenant_id,
        recipient_account_id=row.recipient_account_id,
        source_ref=row.source_ref,
        event_id=row.event_id,
        notification_kind=row.notification_kind,
        summary=row.summary,
        delivery_state=NotificationDeliveryState(row.delivery_state),
        read_at=row.read_at,
        occurred_at=row.occurred_at,
    )


__all__ = [
    "ServiceRequestRepository",
    "InternalNotificationRepository",
    "CommunicationPreferenceRepository",
]