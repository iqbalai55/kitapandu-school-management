"""Regression test for the ORM-INSERT 500 on ``ksma.identity.create_tenant``.

Pre-fix: ORM-generated INSERTs explicitly listed every NOT NULL column
including ``created_at`` and ``updated_at``, supplying ``NULL`` because the
application services don't pass values for them. The DB then rejected with
``null value in column "created_at"`` — even though the DB-level default
``now()`` would have applied had SQLAlchemy omitted the columns.

The fix:
* ``server_default=text("now()")`` on every ORM ``created_at`` / ``updated_at``
  so SQLAlchemy omits them and the DB-side default applies.
* ``default="system"`` on every ORM ``created_by`` / ``updated_by`` so the
  application code never has to remember to pass them.

This file locks in both halves via a single fixture that exercises the
ORM INSERT path (not the raw-SQL seed path) used by the live
``POST /api/v1/admin/tenants`` route.
"""
from __future__ import annotations

import uuid

import pytest
import pytest_asyncio
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from ksma.identity.application import (
    create_education_unit,
    create_tenant,
)
from ksma.identity.infrastructure.models import (
    EducationUnitRow,
    TenantRow,
)


@pytest_asyncio.fixture
async def tenant_via_orm(migrated_db: AsyncEngine):
    """Build a tenant + unit via the ORM INSERT path (which is what the
    public ``POST /api/v1/admin/tenants`` route ultimately calls) and return
    the canonical ids so the tests can read them back via SELECT.
    """
    TestSession = async_sessionmaker(migrated_db, expire_on_commit=False)
    async with TestSession() as session:
        await session.execute(text("SET LOCAL app.bypass_rls = 'true'"))
        # The very call that surfaced the bug pre-fix.
        t = await create_tenant(
            session,
            display_name=f"regression-orm-{uuid.uuid4().hex[:8]}",
            default_timezone="Asia/Jakarta",
        )
        unit = await create_education_unit(
            session,
            tenant_id=t.id,
            unit_code=f"REG-{uuid.uuid4().hex[:4]}",
            name="Regression unit",
            institution_type="lk_lkp",
            education_levels=["TK"],
            timezone="Asia/Jakarta",
        )
        await session.commit()

    return {
        "tenant_id": t.id,
        "unit_id": unit.id,
    }


@pytest.mark.asyncio
async def test_orm_create_tenant_applies_default_audit_columns(
    tenant_via_orm: dict[str, object],
    migrated_db: AsyncEngine,
) -> None:
    """Post-fix: ORM-driven ``create_tenant`` no longer raises a NOT NULL
    on ``created_at``/``updated_at``; defaults flow from SQLAlchemy +
    the Python-side ``default='system'`` on the audit-by columns.
    """
    TestSession = async_sessionmaker(migrated_db, expire_on_commit=False)
    async with TestSession() as session:
        await session.execute(text(
            f"SET LOCAL app.current_tenant = '{tenant_via_orm['tenant_id']}'"
        ))
        row = (await session.execute(
            select(TenantRow).where(TenantRow.id == tenant_via_orm["tenant_id"])
        )).scalar_one()
        assert row.created_at is not None, "DB-side now() did not apply"
        assert row.updated_at is not None, "DB-side now() did not apply"
        assert row.created_by == "system", "Python-side default did not apply"
        assert row.updated_by == "system", "Python-side default did not apply"


@pytest.mark.asyncio
async def test_orm_education_unit_create_passes(
    tenant_via_orm: dict[str, object],
    migrated_db: AsyncEngine,
) -> None:
    """Same regression for EducationUnitRow — the second table the admin
    endpoint exercises.
    """
    TestSession = async_sessionmaker(migrated_db, expire_on_commit=False)
    async with TestSession() as session:
        await session.execute(text(
            f"SET LOCAL app.current_tenant = '{tenant_via_orm['tenant_id']}'"
        ))
        row = (await session.execute(
            select(EducationUnitRow).where(
                EducationUnitRow.id == tenant_via_orm["unit_id"]
            )
        )).scalar_one()
        assert row.created_at is not None
        assert row.updated_at is not None
        assert row.created_by == "system"
        assert row.updated_by == "system"
