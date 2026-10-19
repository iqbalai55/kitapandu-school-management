"""Integration tests for RLS tenant isolation (data dictionary V01).

Per ADR-0001 + ADR-0002 + ticket P1 acceptance: a user in tenant A cannot
read or write any row belonging to tenant B, even when they know the row's
UUID. The Postgres RLS policy + the GUC helpers enforce this.
"""
from __future__ import annotations

import pytest
from sqlalchemy import text


@pytest.mark.asyncio
async def test_v01_cross_tenant_read_isolated(tenant_session) -> None:
    """V01: AC at tenant A asks for a participant at tenant B → empty result."""
    a = tenant_session["tenant_a"]
    b = tenant_session["tenant_b"]

    # Tenant A reads participants: should see only its own.
    rows_a = []
    async for session in tenant_session["session_for"](a):
        rows_a = (await session.execute(
            text("SELECT id, participant_no, full_name FROM participant")
        )).all()
    assert len(rows_a) == 1, f"tenant A should see 1 participant, got {len(rows_a)}"
    assert rows_a[0].full_name == "Sari Tenant A"

    # Tenant B reads participants: should see only its own.
    rows_b = []
    async for session in tenant_session["session_for"](b):
        rows_b = (await session.execute(
            text("SELECT id, participant_no, full_name FROM participant")
        )).all()
    assert len(rows_b) == 1
    assert rows_b[0].full_name == "Sari Tenant B"

    # Crucially, tenant A's row IS in the DB but RLS hides it from B.
    assert rows_a[0].id != rows_b[0].id


@pytest.mark.asyncio
async def test_v01_cross_tenant_write_rejected(tenant_session) -> None:
    """RLS WITH CHECK: a row inserted under tenant A's context must carry
    tenant_id = A; trying to insert with tenant_id = B is rejected by the
    policy. The application layer must NOT bypass this."""
    a = tenant_session["tenant_a"]
    b = tenant_session["tenant_b"]

    # Insert a participant with tenant_id = B while the GUC says current = A.
    # The RLS WITH CHECK clause should reject this.
    async for session in tenant_session["session_for"](a):
        with pytest.raises(Exception):  # RLS raises an InsufficientPrivilege / new row violates policy
            await session.execute(
                text(
                    "INSERT INTO participant (id, tenant_id, participant_no, full_name, status, "
                    "created_at, created_by, updated_at, updated_by) "
                    "VALUES (ksma_uuid(), :wrong_tid, 'P-X-0099', 'Evil Insert', 'active', "
                    "now(), 'seed', now(), 'seed')"
                ),
                {"wrong_tid": str(b)},
            )
            await session.commit()


@pytest.mark.asyncio
async def test_bypass_rls_sees_all_tenants(tenant_session, migrated_db) -> None:
    """When SET LOCAL app.bypass_rls = 'true', the policy yields. Seeds,
    migrations, and audit-cleanup use this path."""
    from sqlalchemy.ext.asyncio import async_sessionmaker
    TestSession = async_sessionmaker(migrated_db, expire_on_commit=False)

    async with TestSession() as session:
        async with session.begin():
            await session.execute(text("SET LOCAL app.bypass_rls = 'true'"))
            rows = (await session.execute(
                text("SELECT id, full_name FROM participant ORDER BY full_name")
            )).all()
            # Two participants from two tenants; both visible.
            assert len(rows) == 2
            names = {r.full_name for r in rows}
            assert "Sari Tenant A" in names
            assert "Sari Tenant B" in names