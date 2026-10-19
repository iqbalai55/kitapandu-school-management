"""Idempotency support.

Per R21 + ADR0001 §6: every protected write endpoint can be called twice with the
same ``Idempotency-Key`` header and the same body without double-effect. A
different body with the same key is rejected.

Storage: ``idempotency_record`` table (U scope — tenant-owned). Columns:

- ``request_subject`` — account/service that initiated
- ``action_id`` — code from the access register (e.g., ``FIN-A05``)
- ``key_fingerprint`` — SHA-256 of the raw ``Idempotency-Key``
- ``request_hash`` — SHA-256 of the canonical request body
- ``status`` — ``in_progress`` | ``succeeded`` | ``failed``
- ``result_ref`` — optional object ref to the side effect (so a replay returns it)
- ``expires_at``
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from ksma.shared.errors import IdempotencyKeyConflictError


@dataclass(frozen=True, slots=True)
class IdempotencyContext:
    """Parsed ``Idempotency-Key`` header + computed fingerprints."""

    raw_key: str
    fingerprint: str  # SHA-256 of the raw key (we don't store the raw key)
    request_hash: str  # SHA-256 of canonical JSON body
    action_id: str
    subject_account_id: UUID
    tenant_id: UUID


def fingerprint_idempotency_key(raw: str) -> str:
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def hash_request_body(body: dict[str, Any] | bytes | str) -> str:
    if isinstance(body, str):
        canonical = body
    elif isinstance(body, bytes):
        canonical = body.decode("utf-8", errors="replace")
    else:
        import json

        canonical = json.dumps(body, sort_keys=True, default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


async def check_or_register(
    session: AsyncSession,
    *,
    ctx: IdempotencyContext,
    ttl_seconds: int = 24 * 3600,
) -> "IdempotencyRecord | None":
    """Look up an existing idempotency record.

    Returns ``None`` if the request should proceed (no prior record or record was
    ``failed``). Returns the existing record if it was ``succeeded`` (caller must
    replay the stored response). Raises ``IdempotencyKeyConflictError`` if a record
    exists with a different request_hash.

    On no match, inserts a new ``in_progress`` row in the same transaction.
    """
    existing = (
        await session.execute(
            text(
                """
                SELECT id, status, result_ref, request_hash
                FROM idempotency_record
                WHERE tenant_id = :tid
                  AND request_subject = :sub
                  AND action_id = :act
                  AND key_fingerprint = :fp
                FOR UPDATE
                """
            ),
            {
                "tid": str(ctx.tenant_id),
                "sub": str(ctx.subject_account_id),
                "act": ctx.action_id,
                "fp": ctx.fingerprint,
            },
        )
    ).first()
    if existing is not None:
        if existing.request_hash != ctx.request_hash:
            raise IdempotencyKeyConflictError(
                "Idempotency-Key reused with a different request body (R21)",
                details={"action_id": ctx.action_id},
            )
        if existing.status == "succeeded":
            return IdempotencyRecord(
                id=existing.id,
                status="succeeded",
                result_ref=existing.result_ref,
            )
        if existing.status == "in_progress":
            # Concurrent request still running; tell the client to retry.
            return IdempotencyRecord(id=existing.id, status="in_progress", result_ref=None)
        # status == "failed": allow the caller to retry — overwrite below
        await session.execute(
            text("DELETE FROM idempotency_record WHERE id = :id"),
            {"id": str(existing.id)},
        )

    expires_at = datetime.now(timezone.utc) + timedelta(seconds=ttl_seconds)
    new_id = uuid4()
    await session.execute(
        text(
            """
            INSERT INTO idempotency_record
              (id, tenant_id, request_subject, action_id,
               key_fingerprint, request_hash, status, expires_at)
            VALUES
              (:id, :tid, :sub, :act, :fp, :rh, 'in_progress', :exp)
            """
        ),
        {
            "id": str(new_id),
            "tid": str(ctx.tenant_id),
            "sub": str(ctx.subject_account_id),
            "act": ctx.action_id,
            "fp": ctx.fingerprint,
            "rh": ctx.request_hash,
            "exp": expires_at,
        },
    )
    return None


async def mark_succeeded(
    session: AsyncSession,
    *,
    record_id: UUID,
    result_ref: str,
) -> None:
    await session.execute(
        text(
            "UPDATE idempotency_record "
            "SET status = 'succeeded', result_ref = :ref "
            "WHERE id = :id"
        ),
        {"ref": result_ref, "id": str(record_id)},
    )


async def mark_failed(
    session: AsyncSession,
    *,
    record_id: UUID,
    reason: str,
) -> None:
    # Keep the row but mark failed for the audit trail; allow the caller to retry
    # with the same key (which will then DELETE and re-INSERT).
    await session.execute(
        text(
            "UPDATE idempotency_record "
            "SET status = 'failed', request_hash = CONCAT(request_hash, ':', :r) "
            "WHERE id = :id"
        ),
        {"r": reason, "id": str(record_id)},
    )


@dataclass(frozen=True, slots=True)
class IdempotencyRecord:
    id: UUID
    status: str  # 'in_progress' | 'succeeded' | 'failed'
    result_ref: str | None