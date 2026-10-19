"""Service-layer audit decorator + helpers.

Per ADR-0004 + R22: every important change writes an ``AuditRecord`` in the
same transaction as the business mutation. The decorator is the contract;
ad-hoc ``INSERT INTO audit_record`` calls outside the decorator are forbidden
in application code (the audit BC routes can still write via a dedicated
service path that the decorator itself uses).
"""
from __future__ import annotations

import functools
import inspect
import json
from collections.abc import Callable as CallableABC
from typing import Any, ParamSpec, TypeVar
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from ksma.shared.tenancy import TenantContext


P = ParamSpec("P")
R = TypeVar("R")


def _json(d: Any) -> str:
    return json.dumps(d, default=str, sort_keys=True)


async def write_audit_record(
    session: AsyncSession,
    *,
    action_id: str,
    object_ref: dict[str, Any],
    ctx: TenantContext | None,
    correlation_id: str,
    authorization_ref: dict[str, Any] | None = None,
    change_ref: dict[str, Any] | None = None,
    reason: str | None = None,
) -> UUID | None:
    """Write one ``AuditRecord`` row in the current transaction.

    Returns the row id, or ``None`` if no tenant context was provided (in
    which case we record the actor as ``system:audit-stale`` so the row is
    still attributable to *something* rather than dropped silently).
    """
    if ctx is None:
        actor = {"kind": "system", "id": "audit-stale"}
        tenant_id = None
        authorization_ref = authorization_ref or {"kind": "system"}
    else:
        actor = {"kind": "account", "id": str(ctx.account_id)}
        tenant_id = ctx.tenant_id
        authorization_ref = authorization_ref or {
            "kind": "membership",
            "id": str(ctx.membership_id),
            "role_code": ctx.role_code,
        }
    row = (
        await session.execute(
            text(
                """
                INSERT INTO audit_record
                  (tenant_id, actor, action_id, object_ref, change_ref,
                   reason, authorization_ref, correlation_id)
                VALUES
                  (:tid, CAST(:actor AS JSONB), :action_id, CAST(:obj AS JSONB),
                   CAST(:change AS JSONB), :reason, CAST(:auth AS JSONB), :cid)
                RETURNING id
                """
            ),
            {
                "tid": str(tenant_id) if tenant_id else None,
                "actor": _json(actor),
                "action_id": action_id,
                "obj": _json(object_ref),
                "change": _json(change_ref) if change_ref else None,
                "reason": reason,
                "auth": _json(authorization_ref),
                "cid": correlation_id,
            },
        )
    ).first()
    return row.id if row else None


def audited(
    action_id: str,
    *,
    object_kind: str | None = None,
    object_id_arg: str | None = None,
    reason_required: bool = False,
    change_ref_arg: str | None = None,
) -> CallableABC[[CallableABC[P, R]], CallableABC[P, R]]:
    """Decorator: wrap a service-layer async function with audit + reason enforcement.

    The wrapped function must take ``session: AsyncSession`` as its first
    positional argument and ``ctx: TenantContext`` as its second positional
    argument. The function must return something with ``.id`` (the audit row
    will record that as ``object_ref.id``).

    Usage::

        @audited("REG-A04", object_kind="application", object_id_arg=None,
                 reason_required=True, change_ref_arg="prior_state")
        async def decide_application(session, ctx, *, application_id, decision, reason, prior_state):
            ...

    ``object_id_arg`` is the name of a kwarg in the function whose value is
    the target object's id; defaults to the second-to-last positional arg of
    the return value's ``.id``.
    """
    def decorator(fn: CallableABC[P, R]) -> CallableABC[P, R]:
        sig = inspect.signature(fn)
        reason_arg = next(
            (n for n in ("reason", "correction_reason", "revocation_reason")
             if n in sig.parameters),
            None,
        )

        @functools.wraps(fn)
        async def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
            session: AsyncSession = args[0]
            ctx: TenantContext | None = args[1] if len(args) > 1 else kwargs.get("ctx")
            if reason_required and reason_arg:
                if not kwargs.get(reason_arg):
                    from ksma.shared.errors import ValidationError
                    raise ValidationError(
                        f"reason is required for action {action_id}"
                    )
            correlation_id = getattr(ctx, "correlation_id", "unknown") if ctx else "unknown"
            result = await fn(*args, **kwargs)
            # Build the object_ref from result.id by default, or from a kwarg.
            obj: dict[str, Any] = {"kind": object_kind or "unknown"}
            target_id: Any = None
            if object_id_arg and object_id_arg in kwargs:
                target_id = kwargs[object_id_arg]
            elif result is not None and hasattr(result, "id"):
                target_id = result.id
            if target_id is not None:
                obj["id"] = str(target_id)
            change_ref: dict[str, Any] | None = None
            if change_ref_arg and change_ref_arg in kwargs:
                change_ref = kwargs[change_ref_arg]
                if not isinstance(change_ref, dict):
                    change_ref = {"value": change_ref}
            reason_value = kwargs.get(reason_arg) if reason_arg else None
            await write_audit_record(
                session,
                action_id=action_id,
                object_ref=obj,
                ctx=ctx,
                correlation_id=correlation_id,
                change_ref=change_ref,
                reason=reason_value,
            )
            return result

        return wrapper

    return decorator


__all__ = ["write_audit_record", "audited"]