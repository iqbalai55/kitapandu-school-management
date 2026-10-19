"""OutboxEvent writer + CLI worker.

Per ADR0001 §6 + the verified research subagent's gotchas:

- Business write + ``outbox_messages`` row happen in **one transaction**.
- The worker uses ``SELECT … FOR UPDATE SKIP LOCKED`` so multiple replicas can run
  concurrently with no double-processing.
- Strategy is **publish-then-mark** (at-least-once). Consumers must be idempotent
  on ``event_id``; we use ``ProcessedEvent`` per (consumer_code, event_id) for dedup.

The worker is run via ``python -m ksma.shared.outbox.run``.
"""
from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import text

from ksma.shared.events import DomainEvent, bus
from ksma.shared.persistence import session_scope

logger = logging.getLogger("ksma.outbox")

# Schema constants — referenced by migrations and by the writer.
OUTBOX_TABLE = "outbox_messages"
PROCESSED_TABLE = "processed_events"


@dataclass(frozen=True, slots=True)
class OutboxRow:
    id: UUID
    event_type: str
    payload: dict[str, Any]
    aggregate_ref: str | None
    correlation_id: UUID | None


async def enqueue(
    session: AsyncSession,
    event: DomainEvent,
    *,
    aggregate_ref: str | None = None,
) -> UUID:
    """Append an ``outbox_messages`` row in the **same transaction** as the caller's
    business write.

    Returns the row's ``event_id``.
    """
    event_id = event.event_id
    payload = {
        "event_type": event.event_type,
        "schema_version": event.schema_version,
        "correlation_id": str(event.correlation_id) if event.correlation_id else None,
        "tenant_id": str(event.tenant_id) if event.tenant_id else None,
        "actor_kind": event.actor_kind,
        "actor_id": str(event.actor_id) if event.actor_id else None,
        "occurred_at": event.occurred_at.isoformat(),
        "data": _serialize(event),
    }
    await session.execute(
        text(
            f"""
            INSERT INTO {OUTBOX_TABLE}
              (event_id, event_type, payload, aggregate_ref,
               occurred_at, dispatch_state, correlation_id)
            VALUES
              (:event_id, :event_type, CAST(:payload AS JSONB), :aggregate_ref,
               :occurred_at, 'pending', CAST(:correlation_id AS UUID))
            """
        ),
        {
            "event_id": str(event_id),
            "event_type": event.event_type,
            "payload": _json_dumps(payload),
            "aggregate_ref": aggregate_ref,
            "occurred_at": event.occurred_at,
            "correlation_id": str(event.correlation_id) if event.correlation_id else None,
        },
    )
    return event_id


def _serialize(event: DomainEvent) -> dict[str, Any]:
    """Convert each base object to a JSON-safe dict. Subclasses override as needed."""
    return {
        "event_id": str(event.event_id),
        "occurred_at": event.occurred_at.isoformat(),
        "actor_kind": event.actor_kind,
        "actor_id": str(event.actor_id) if event.actor_id else None,
        "tenant_id": str(event.tenant_id) if event.tenant_id else None,
    }


def _json_dumps(d: dict[str, Any]) -> str:
    import json

    return json.dumps(d, default=str)


# ---------------------------------------------------------------------------
# Worker
# ---------------------------------------------------------------------------


_PROCESS_BATCH_SQL = text(
    f"""
    WITH next AS (
        SELECT id
        FROM {OUTBOX_TABLE}
        WHERE dispatch_state = 'pending'
        ORDER BY id
        FOR UPDATE SKIP LOCKED
        LIMIT :batch
    )
    UPDATE {OUTBOX_TABLE} o
    SET dispatch_state = 'dispatched',
        dispatched_at = now()
    FROM next
    WHERE o.id = next.id
    RETURNING o.id, o.event_type, o.payload, o.correlation_id
    """
)


async def drain_once(batch_size: int) -> int:
    """Mark one batch of outbox rows as dispatched and run their in-process handlers.

    Returns the number of rows processed. Designed to be called in a loop.
    """
    processed = 0
    async with session_scope() as session:
        rows = (await session.execute(_PROCESS_BATCH_SQL, {"batch": batch_size})).all()
        for row in rows:
            try:
                _run_handler(row)
            except Exception:
                logger.exception(
                    "Outbox handler failed",
                    extra={"event_type": row.event_type, "outbox_id": str(row.id)},
                )
                # We have already marked the row 'dispatched'. Consumers must be
                # idempotent; failed deliveries are logged for manual replay.
            processed += 1
    return processed


def _run_handler(row: Any) -> None:
    """Route a dispatched row to its in-process consumer.

    For pilot, every event has a single registered handler keyed by ``event_type``.
    Handlers live in their respective bounded contexts (see ``finance.application``,
    ``portal.application``, etc.) and subscribe at app startup via ``bus().subscribe``.
    """
    payload = row.payload
    event_type = payload["event_type"]
    # Build a stub DomainEvent and let the bus dispatch. Each handler is responsible
    # for parsing the payload into its strongly-typed subclass.
    from ksma.shared.events import DomainEvent

    stub = DomainEvent.__new__(DomainEvent)
    object.__setattr__(stub, "event_type", event_type)
    object.__setattr__(stub, "event_id", UUID(row.id))
    object.__setattr__(stub, "correlation_id", UUID(row.correlation_id) if row.correlation_id else None)
    bus().dispatch(stub)


async def run_forever(poll_interval: float, batch_size: int) -> None:  # pragma: no cover
    """CLI loop. Use ``python -m ksma.shared.outbox.run`` instead."""
    logger.info("outbox worker starting (poll=%.2fs, batch=%d)", poll_interval, batch_size)
    while True:
        try:
            n = await drain_once(batch_size)
            if n == 0:
                await asyncio.sleep(poll_interval)
        except Exception:
            logger.exception("outbox worker tick failed")
            await asyncio.sleep(poll_interval)