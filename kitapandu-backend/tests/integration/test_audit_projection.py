"""Integration tests for the audit-projection consumer (P4.1 sub-area #2).

Per spec 000 / issue #31 P4.1: when finance / session / reporting emit a
past-tense event, the portal's audit-projection consumer must write one
``internal_notification`` row per recipient, deduped on the UNIQUE
``(tenant_id, recipient_account_id, event_id, notification_kind)``
constraint (V21).

These tests exercise ``portal.application.dispatch_event_to_inbox``
directly. The bus-driven path (``portal.interface.audit_projection``)
schedules the same call asynchronously; the on-the-wire correctness is
verified here via the public application service.
"""
from __future__ import annotations

import uuid
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from ksma.portal.application import dispatch_event_to_inbox
from ksma.shared.tenancy import ActorKind, TenantContext


# ---------------------------------------------------------------------------
# Fixture — single tenant + AC + FI accounts/memberships/grants per test
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def audit_proj_setup(migrated_db: AsyncEngine):
    """Seed one tenant + unit + finance policy + two accounts (AC + FI)
    with active memberships + active access_assignments for the given
    role_codes. Yields (engine, tenant_id, account_ids, ctx).
    """
    tenant_id = uuid.uuid4()
    unit_id = uuid.uuid4()
    policy_id = uuid.uuid4()
    ac_account_id = uuid.uuid4()
    fi_account_id = uuid.uuid4()
    ac_membership_id = uuid.uuid4()
    fi_membership_id = uuid.uuid4()

    TestSession = async_sessionmaker(migrated_db, expire_on_commit=False)
    async with TestSession() as session:
        await session.execute(text("SET LOCAL app.bypass_rls = 'true'"))
        # Tenant + unit
        await session.execute(
            text(
                "INSERT INTO tenant (id, display_name, service_status, default_timezone, "
                "created_at, created_by, updated_at, updated_by) "
                "VALUES (:tid, 'Tenant AuditProj', 'active', 'Asia/Jakarta', "
                "now(), 'seed', now(), 'seed')"
            ),
            {"tid": str(tenant_id)},
        )
        await session.execute(
            text(
                "INSERT INTO education_unit "
                "(id, tenant_id, unit_code, name, institution_type, education_levels, "
                " status, timezone, created_at, created_by, updated_at, updated_by) "
                "VALUES "
                "(:uid, :tid, 'AUD-A', 'AuditProj Unit', 'lk_lkp', ARRAY['TK'], "
                " 'active', 'Asia/Jakarta', now(), 'seed', now(), 'seed')"
            ),
            {"uid": str(unit_id), "tid": str(tenant_id)},
        )
        # Policy (FK target for access_assignment)
        await session.execute(
            text(
                "INSERT INTO policy_version "
                "(id, tenant_id, policy_domain, version_no, effective_range, ruleset, status, "
                "created_at, created_by, updated_at, updated_by) "
                "VALUES "
                "(:pid, :tid, 'access', 1, tstzrange(now(), 'infinity'), "
                " CAST('{}' AS JSONB), 'published', "
                " now(), 'seed', now(), 'seed')"
            ),
            {"pid": str(policy_id), "tid": str(tenant_id)},
        )
        # Two accounts + two memberships
        for account_id, auth_subject in (
            (ac_account_id, "ac-user"),
            (fi_account_id, "fi-user"),
        ):
            await session.execute(
                text(
                    "INSERT INTO account "
                    "(id, auth_subject, auth_namespace, display_name, status, authz_revision, "
                    "created_at, created_by, updated_at, updated_by) "
                    "VALUES "
                    "(:id, :sub, 'default', :dn, 'active', 1, "
                    " now(), 'seed', now(), 'seed')"
                ),
                {"id": str(account_id), "sub": auth_subject, "dn": auth_subject},
            )
        for membership_id, account_id in (
            (ac_membership_id, ac_account_id),
            (fi_membership_id, fi_account_id),
        ):
            await session.execute(
                text(
                    "INSERT INTO membership "
                    "(id, tenant_id, account_id, status, invitation_ref, "
                    "created_at, created_by, updated_at, updated_by) "
                    "VALUES "
                    "(:id, :tid, :aid, 'active', :ref, "
                    " now(), 'seed', now(), 'seed')"
                ),
                {
                    "id": str(membership_id),
                    "tid": str(tenant_id),
                    "aid": str(account_id),
                    "ref": f"seed-{account_id.hex[:8]}",
                },
            )
        # Two active access_assignments: one AC, one FI.
        for membership_id, role_code in (
            (ac_membership_id, "AC"),
            (fi_membership_id, "FI"),
        ):
            await session.execute(
                text(
                    "INSERT INTO access_assignment "
                    "(id, tenant_id, membership_id, role_code, action_ids, scope, "
                    "valid_from, status, policy_id, \"authorization\", "
                    "created_at, created_by, updated_at, updated_by) "
                    "VALUES "
                    "(ksma_uuid(), :tid, :mid, :rc, ARRAY[]::text[], "
                    " CAST('{}' AS JSONB), now(), 'active', :pid, "
                    " CAST('{\"actor\":\"seed\"}' AS JSONB), "
                    " now(), 'seed', now(), 'seed')"
                ),
                {
                    "tid": str(tenant_id),
                    "mid": str(membership_id),
                    "rc": role_code,
                    "pid": str(policy_id),
                },
            )
        await session.commit()

    ctx = TenantContext(
        account_id=ac_account_id,  # any account — used only for tenant_id
        membership_id=ac_membership_id,
        tenant_id=tenant_id,
        unit_id=unit_id,
        actor_kind=ActorKind.ACCOUNT,
        authz_revision=1,
        role_codes=("AC",),
    )
    return {
        "tenant_id": tenant_id,
        "ctx": ctx,
        "ac_account_id": ac_account_id,
        "fi_account_id": fi_account_id,
        "ac_membership_id": ac_membership_id,
        "fi_membership_id": fi_membership_id,
    }


async def _count_rows(session, tenant_id: uuid.UUID, kind: str | None = None) -> int:
    params: dict[str, Any] = {"tid": str(tenant_id)}
    sql = (
        "SELECT COUNT(*) AS n FROM internal_notification "
        "WHERE tenant_id = :tid"
    )
    if kind is not None:
        sql += " AND notification_kind = :kind"
        params["kind"] = kind
    row = (await session.execute(text(sql), params)).first()
    return int(row.n)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_audit_projection_writes_one_row_per_recipient(
    audit_proj_setup, migrated_db: AsyncEngine
) -> None:
    """First dispatch with two recipients → exactly two rows land."""
    tenant_id = audit_proj_setup["tenant_id"]
    ctx = audit_proj_setup["ctx"]
    recipients = [
        audit_proj_setup["ac_account_id"],
        audit_proj_setup["fi_account_id"],
    ]
    event_id = uuid.uuid4()
    TestSession = async_sessionmaker(migrated_db, expire_on_commit=False)
    async with TestSession() as session:
        await session.execute(
            text(f"SET LOCAL app.current_tenant = '{tenant_id}'")
        )
        inserted = await dispatch_event_to_inbox(
            session,
            ctx,
            event_type="InvoiceIssued",
            event_id=event_id,
            notification_kind="finance.invoice_issued",
            summary="Invoice INV-AUD-0001 issued",
            source_ref={"kind": "invoice", "invoice_no": "INV-AUD-0001"},
            recipient_account_ids=recipients,
        )
        await session.commit()
    assert inserted == 2

    async with TestSession() as session:
        await session.execute(
            text(f"SET LOCAL app.current_tenant = '{tenant_id}'")
        )
        assert await _count_rows(session, tenant_id) == 2
        assert (
            await _count_rows(session, tenant_id, kind="finance.invoice_issued") == 2
        )


@pytest.mark.asyncio
async def test_audit_projection_v21_dedup_same_event_and_kind(
    audit_proj_setup, migrated_db: AsyncEngine
) -> None:
    """Re-dispatch with the same event_id + same kind → zero new rows.

    The UNIQUE (tenant_id, recipient_account_id, event_id, notification_kind)
    constraint (V21) suppresses the second insert.
    """
    tenant_id = audit_proj_setup["tenant_id"]
    ctx = audit_proj_setup["ctx"]
    recipients = [
        audit_proj_setup["ac_account_id"],
        audit_proj_setup["fi_account_id"],
    ]
    event_id = uuid.uuid4()
    TestSession = async_sessionmaker(migrated_db, expire_on_commit=False)

    # First dispatch — 2 rows land.
    async with TestSession() as session:
        await session.execute(
            text(f"SET LOCAL app.current_tenant = '{tenant_id}'")
        )
        first = await dispatch_event_to_inbox(
            session,
            ctx,
            event_type="InvoiceIssued",
            event_id=event_id,
            notification_kind="finance.invoice_issued",
            summary="Invoice INV-AUD-0001 issued",
            source_ref={"kind": "invoice", "invoice_no": "INV-AUD-0001"},
            recipient_account_ids=recipients,
        )
        await session.commit()
    assert first == 2

    # Second dispatch — same event_id, same kind, same recipients.
    async with TestSession() as session:
        await session.execute(
            text(f"SET LOCAL app.current_tenant = '{tenant_id}'")
        )
        second = await dispatch_event_to_inbox(
            session,
            ctx,
            event_type="InvoiceIssued",
            event_id=event_id,
            notification_kind="finance.invoice_issued",
            summary="Invoice INV-AUD-0001 issued (redelivered)",
            source_ref={"kind": "invoice", "invoice_no": "INV-AUD-0001"},
            recipient_account_ids=recipients,
        )
        await session.commit()
    assert second == 0  # V21 dedup — zero new rows

    async with TestSession() as session:
        await session.execute(
            text(f"SET LOCAL app.current_tenant = '{tenant_id}'")
        )
        assert await _count_rows(session, tenant_id) == 2


@pytest.mark.asyncio
async def test_audit_projection_new_event_id_lands_a_fresh_row(
    audit_proj_setup, migrated_db: AsyncEngine
) -> None:
    """Same kind but a different event_id → one fresh row per recipient.

    V21 dedup keys on (tenant, recipient, event_id, kind) — a new event_id
    produces a new tuple, so the insert is not suppressed.
    """
    tenant_id = audit_proj_setup["tenant_id"]
    ctx = audit_proj_setup["ctx"]
    ac_id = audit_proj_setup["ac_account_id"]
    fi_id = audit_proj_setup["fi_account_id"]
    TestSession = async_sessionmaker(migrated_db, expire_on_commit=False)

    event_v1 = uuid.uuid4()
    event_v2 = uuid.uuid4()

    async with TestSession() as session:
        await session.execute(
            text(f"SET LOCAL app.current_tenant = '{tenant_id}'")
        )
        first = await dispatch_event_to_inbox(
            session,
            ctx,
            event_type="PaymentVerified",
            event_id=event_v1,
            notification_kind="finance.payment_verified",
            summary="Payment INV-AUD-0001 verified",
            source_ref={"event_no": 1},
            recipient_account_ids=[ac_id, fi_id],
        )
        await session.commit()
    assert first == 2

    async with TestSession() as session:
        await session.execute(
            text(f"SET LOCAL app.current_tenant = '{tenant_id}'")
        )
        second = await dispatch_event_to_inbox(
            session,
            ctx,
            event_type="PaymentVerified",
            event_id=event_v2,  # new event_id
            notification_kind="finance.payment_verified",
            summary="Payment INV-AUD-0002 verified",
            source_ref={"event_no": 2},
            recipient_account_ids=[ac_id, fi_id],
        )
        await session.commit()
    assert second == 2  # two new rows — V21 dedup is per event_id

    async with TestSession() as session:
        await session.execute(
            text(f"SET LOCAL app.current_tenant = '{tenant_id}'")
        )
        assert await _count_rows(session, tenant_id) == 4
        assert (
            await _count_rows(session, tenant_id, kind="finance.payment_verified")
            == 4
        )


@pytest.mark.asyncio
async def test_audit_projection_no_recipients_is_a_noop(
    audit_proj_setup, migrated_db: AsyncEngine
) -> None:
    """An empty recipient list writes nothing."""
    tenant_id = audit_proj_setup["tenant_id"]
    ctx = audit_proj_setup["ctx"]
    TestSession = async_sessionmaker(migrated_db, expire_on_commit=False)
    async with TestSession() as session:
        await session.execute(
            text(f"SET LOCAL app.current_tenant = '{tenant_id}'")
        )
        inserted = await dispatch_event_to_inbox(
            session,
            ctx,
            event_type="InvoiceIssued",
            event_id=uuid.uuid4(),
            notification_kind="finance.invoice_issued",
            summary="nothing",
            source_ref={},
            recipient_account_ids=[],
        )
        await session.commit()
    assert inserted == 0

    async with TestSession() as session:
        await session.execute(
            text(f"SET LOCAL app.current_tenant = '{tenant_id}'")
        )
        assert await _count_rows(session, tenant_id) == 0


@pytest.mark.asyncio
async def test_audit_projection_resolves_recipients_via_active_grants(
    migrated_db: AsyncEngine,
) -> None:
    """``_resolve_recipients`` returns accounts that have an active
    ``access_assignment`` with one of the configured role_codes (AC/FI).
    Suspended / revoked memberships + non-matching role_codes are excluded.
    """
    from ksma.portal.interface.audit_projection import (
        DEFAULT_NOTIFY_ROLES,
        _resolve_recipients,
    )

    tenant_id = uuid.uuid4()
    policy_id = uuid.uuid4()
    ac_acct = uuid.uuid4()
    fi_acct = uuid.uuid4()
    st_acct = uuid.uuid4()
    suspended_acct = uuid.uuid4()
    revoked_acct = uuid.uuid4()
    TestSession = async_sessionmaker(migrated_db, expire_on_commit=False)

    async with TestSession() as session:
        await session.execute(text("SET LOCAL app.bypass_rls = 'true'"))
        await session.execute(
            text(
                "INSERT INTO tenant (id, display_name, service_status, default_timezone, "
                "created_at, created_by, updated_at, updated_by) "
                "VALUES (:tid, 'Tenant Resolve', 'active', 'Asia/Jakarta', "
                "now(), 'seed', now(), 'seed')"
            ),
            {"tid": str(tenant_id)},
        )
        await session.execute(
            text(
                "INSERT INTO policy_version "
                "(id, tenant_id, policy_domain, version_no, effective_range, ruleset, status, "
                "created_at, created_by, updated_at, updated_by) "
                "VALUES "
                "(:pid, :tid, 'access', 1, tstzrange(now(), 'infinity'), "
                " CAST('{}' AS JSONB), 'published', now(), 'seed', now(), 'seed')"
            ),
            {"pid": str(policy_id), "tid": str(tenant_id)},
        )
        memberships: list[tuple[uuid.UUID, uuid.UUID, str]] = []
        # account, role_code, membership status
        for acct, role, mem_status in (
            (ac_acct, "AC", "active"),
            (fi_acct, "FI", "active"),
            (st_acct, "ST", "active"),
            (suspended_acct, "AC", "suspended"),
            (revoked_acct, "FI", "revoked"),
        ):
            await session.execute(
                text(
                    "INSERT INTO account "
                    "(id, auth_subject, auth_namespace, display_name, status, authz_revision, "
                    "created_at, created_by, updated_at, updated_by) "
                    "VALUES "
                    "(:id, :sub, 'default', :dn, 'active', 1, "
                    " now(), 'seed', now(), 'seed')"
                ),
                {"id": str(acct), "sub": f"u-{acct.hex[:8]}", "dn": role},
            )
            mid = uuid.uuid4()
            memberships.append((mid, acct, role))
            await session.execute(
                text(
                    "INSERT INTO membership "
                    "(id, tenant_id, account_id, status, invitation_ref, "
                    "created_at, created_by, updated_at, updated_by) "
                    "VALUES (:id, :tid, :aid, :status, :ref, "
                    " now(), 'seed', now(), 'seed')"
                ),
                {
                    "id": str(mid),
                    "tid": str(tenant_id),
                    "aid": str(acct),
                    "status": mem_status,
                    "ref": f"seed-{acct.hex[:8]}",
                },
            )
            # Active assignment only for the "active" memberships.
            if mem_status == "active":
                await session.execute(
                    text(
                        "INSERT INTO access_assignment "
                        "(id, tenant_id, membership_id, role_code, action_ids, scope, "
                        "valid_from, status, policy_id, \"authorization\", "
                        "created_at, created_by, updated_at, updated_by) "
                        "VALUES "
                        "(ksma_uuid(), :tid, :mid, :rc, ARRAY[]::text[], "
                        " CAST('{}' AS JSONB), now(), 'active', :pid, "
                        " CAST('{\"actor\":\"seed\"}' AS JSONB), "
                        " now(), 'seed', now(), 'seed')"
                    ),
                    {
                        "tid": str(tenant_id),
                        "mid": str(mid),
                        "rc": role,
                        "pid": str(policy_id),
                    },
                )
        await session.commit()

    async with TestSession() as session:
        await session.execute(text("SET LOCAL app.bypass_rls = 'true'"))
        recipients = set(
            await _resolve_recipients(session, tenant_id, DEFAULT_NOTIFY_ROLES)
        )
    # AC + FI active assignments; ST is in DEFAULT_NOTIFY_ROLES too.
    # suspended/revoked memberships excluded.
    assert ac_acct in recipients
    assert fi_acct in recipients
    assert st_acct in recipients
    assert suspended_acct not in recipients
    assert revoked_acct not in recipients