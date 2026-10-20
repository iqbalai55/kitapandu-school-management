"""Audit-projection subscribers — outbox / in-process bus → ``internal_notification``.

Per ADR-0001 §6 + ticket P4.1 sub-area #2: every past-tense domain event written
to ``outbox_messages`` by the bounded contexts (finance / session / reporting)
must produce one ``internal_notification`` row per recipient, deduped on the
``(tenant_id, recipient_account_id, event_id, notification_kind)`` UNIQUE
constraint (V21).

The in-process ``EventBus`` runs handlers synchronously after ``enqueue`` (see
``finance.application.issue_invoice`` and friends). The handler here is
sync, so we cannot await a DB write inline. We therefore fire-and-forget an
async task that opens its own session and projects the event. The UNIQUE
constraint makes the projection idempotent — a redelivered (same
``outbox_messages`` row, same ``event_id``) event produces zero new rows.

Event-type → notification-kind mapping (per spec 000 / issue #31 P4.1):

* ``InvoiceIssued``      → ``finance.invoice_issued``
* ``PaymentVerified``    → ``finance.payment_verified``
* ``FundsAllocated``     → ``finance.funds_allocated``
* ``SessionScheduled``   → ``session.scheduled``
* ``ReportPublished``    → ``reporting.published``

Recipient resolution for pilot is intentionally simple: every account in the
event's tenant that has an active ``access_assignment`` with one of the
``AC`` / ``FI`` / ``GU`` / ``ST`` / ``PY`` role codes. Refinements (per
event-type scoping, e.g. only participants on a given invoice) can be added
later without changing the public surface.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from ksma.portal.application import dispatch_event_to_inbox
from ksma.shared.events import DomainEvent, bus
from ksma.shared.persistence import session_scope, set_current_tenant
from ksma.shared.tenancy import ActorKind, TenantContext


logger = logging.getLogger("ksma.portal.audit_projection")


# Roles whose accounts get a notification for any tenant-wide audit event.
# Matches the spec's P4.1 mapping: AC + IN for sessions, GU + PY + ST for
# reports, AC + FI for finance, etc. For pilot we default to the superset.
DEFAULT_NOTIFY_ROLES: tuple[str, ...] = ("AC", "FI", "GU", "ST", "PY")


async def _resolve_recipients(
    session: AsyncSession,
    tenant_id: UUID,
    role_codes: tuple[str, ...],
) -> list[UUID]:
    """Return distinct account_ids in ``tenant_id`` whose active membership
    has an active ``access_assignment`` with one of the given ``role_codes``.
    """
    if not role_codes:
        return []
    rows = (
        await session.execute(
            text(
                """
                SELECT DISTINCT m.account_id AS account_id
                FROM membership m
                JOIN access_assignment aa ON aa.membership_id = m.id
                WHERE m.tenant_id = :tid
                  AND m.status = 'active'
                  AND aa.status = 'active'
                  AND aa.role_code = ANY(CAST(:codes AS text[]))
                """
            ),
            {"tid": str(tenant_id), "codes": list(role_codes)},
        )
    ).all()
    return [r.account_id for r in rows]


async def _project_event_to_inbox(
    *,
    tenant_id: UUID,
    event_id: UUID,
    event_type: str,
    notification_kind: str,
    summary: str,
    source_ref: dict[str, Any],
    role_codes: tuple[str, ...],
) -> int:
    """Open a fresh session, resolve recipients, dispatch_event_to_inbox.

    Runs in its own DB transaction so the projection is decoupled from the
    business-write transaction. The UNIQUE constraint on
    ``internal_notification`` keeps this idempotent across redeliveries.
    """
    async with session_scope() as session:
        await set_current_tenant(session, tenant_id)
        recipients = await _resolve_recipients(session, tenant_id, role_codes)
        if not recipients:
            return 0
        # The audit projection is initiated by a domain event, not by an
        # account — use a service-principal-shaped context so the tenant
        # context is correctly populated but ``account_id`` is None.
        ctx = TenantContext(
            account_id=None,
            membership_id=None,
            tenant_id=tenant_id,
            unit_id=None,
            actor_kind=ActorKind.SERVICE_PRINCIPAL,
            authz_revision=0,
            role_codes=role_codes,
        )
        return await dispatch_event_to_inbox(
            session,
            ctx,
            event_type=event_type,
            event_id=event_id,
            notification_kind=notification_kind,
            summary=summary,
            source_ref=source_ref,
            recipient_account_ids=recipients,
        )


def _make_handler(event_type: str, notification_kind: str):
    """Build a sync bus handler that schedules an async projection task.

    The bus dispatches synchronously (``EventBus.dispatch`` is sync), so the
    handler cannot ``await`` inline. We schedule the projection on the
    currently-running asyncio loop via ``asyncio.create_task``. Outside an
    event loop (e.g. inside the outbox worker's ``drain_once`` which runs
    from ``asyncio.run``) the loop is still available, so this works there
    too. Inside a worker without a running loop we log and skip.
    """

    def handler(event: DomainEvent) -> None:
        tenant_id = event.tenant_id
        if tenant_id is None:
            logger.debug(
                "audit projection: skipping event without tenant_id",
                extra={"event_type": event_type, "event_id": str(event.event_id)},
            )
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            logger.warning(
                "audit projection: no running event loop, skipping",
                extra={"event_type": event_type, "event_id": str(event.event_id)},
            )
            return
        loop.create_task(
            _project_event_to_inbox(
                tenant_id=tenant_id,
                event_id=event.event_id,
                event_type=event_type,
                notification_kind=notification_kind,
                summary=f"{event_type} ({event.event_id})",
                source_ref={
                    "event_type": event_type,
                    "event_id": str(event.event_id),
                },
                role_codes=DEFAULT_NOTIFY_ROLES,
            )
        )

    return handler


# Mapping table — kept module-level so it's easy to extend without changing
# the registration code.
_PROJECTION_MAP: dict[type[DomainEvent], tuple[str, str]] = {}


def register_audit_projection_subscribers() -> None:
    """Subscribe each event-type in the audit-projection mapping to its handler.

    Idempotent: safe to call multiple times. The first registration wins
    because ``bus().subscribe`` appends, but in practice ``main.create_app``
    is the only caller.
    """
    # Late imports keep portal decoupled from finance / session / reporting.
    # These imports are intentional — the projection consumer *is* the
    # integration point for those contexts.
    from ksma.finance.domain.events import (
        FundsAllocated,
        InvoiceIssued,
        PaymentVerified,
    )
    from ksma.reporting.domain.events import ReportPublished
    from ksma.session.domain.events import SessionScheduled

    _PROJECTION_MAP.update(
        {
            InvoiceIssued: ("InvoiceIssued", "finance.invoice_issued"),
            PaymentVerified: ("PaymentVerified", "finance.payment_verified"),
            FundsAllocated: ("FundsAllocated", "finance.funds_allocated"),
            SessionScheduled: ("SessionScheduled", "session.scheduled"),
            ReportPublished: ("ReportPublished", "reporting.published"),
        }
    )

    for event_cls, (event_type, notification_kind) in _PROJECTION_MAP.items():
        bus().subscribe(event_cls, _make_handler(event_type, notification_kind))


__all__ = [
    "DEFAULT_NOTIFY_ROLES",
    "register_audit_projection_subscribers",
]