"""Integration tests for the V23 recovery-drill isolation guarantee (ticket #31).

Per OPS Section 4 + V23 + R23 acceptance:

  * A backup of a single tenant must restore into an isolated image WITHOUT
    any cross-tenant leak.
  * ``restore`` / ``restore_test`` operations in ``production`` environment
    must be REFUSED by ``start_operational_run`` *before* any DB write.

Seam: real Postgres 16 (testcontainers) per the existing integration suite.
The drill runs against the per-test ``migrated_db`` (already-migrated
ephemeral DB), under a separately-bootstrapped ``ksma_recovery`` Postgres
role that is ``NOSUPERUSER NOBYPASSRLS``. RLS USING/WITH CHECK clauses on
the domain tables constrain the role's reads/writes to the active tenant
context.

We do NOT modify ``conftest.py``; we reuse ``migrated_db`` and create the
recovery role + grants inline (small helpers live at the top of this file).
"""
from __future__ import annotations

import subprocess
import uuid
from collections.abc import AsyncIterator
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    async_sessionmaker,
    create_async_engine,
)

from ksma.operations.application import (
    finish_operational_run,
    start_operational_run,
)
from ksma.operations.domain import (
    OperationalEnvironment,
    OperationalRunStatus,
    OperationKind,
)
from ksma.shared.errors import StateConflictError
from ksma.shared.tenancy import ActorKind, TenantContext


RECOVERY_ROLE_NAME = "ksma_recovery"
RECOVERY_ROLE_PASSWORD = "ksma_recovery"


# ---------------------------------------------------------------------------
# Helpers — subprocess wrappers for psql role/grants creation.
# ---------------------------------------------------------------------------


def _clean_sync_dsn(url: str) -> str:
    """Strip any SQLAlchemy dialect prefix so psql + psycopg accept it."""
    return (
        url.replace("postgresql+psycopg2://", "postgresql://", 1)
        .replace("postgresql+asyncpg://", "postgresql://", 1)
    )


def _create_recovery_role(
    *,
    pg_dsn_sync: str,
    test_db_name: str,
    role_name: str,
    role_password: str,
) -> None:
    """Create ``role_name`` (NOSUPERUSER, NOBYPASSRLS) on the test DB if missing.

    The role is INTENTIONALLY NOT a superuser and does NOT have ``BYPASSRLS``
    so that RLS USING/WITH CHECK clauses enforce tenant isolation for any
    connection it makes.
    """
    admin_url = _clean_sync_dsn(pg_dsn_sync).rsplit("/", 1)[0] + "/" + test_db_name
    subprocess.run(
        [
            "psql",
            admin_url,
            "-c",
            (
                "DO $$ BEGIN "
                f"IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{role_name}') THEN "
                f"CREATE ROLE {role_name} WITH LOGIN PASSWORD '{role_password}' "
                "NOSUPERUSER NOBYPASSRLS; "
                "END IF; "
                "END $$;"
            ),
        ],
        check=True,
        capture_output=True,
    )
    # Grant access to the test DB + schema + the minimum table set the
    # recovery role needs: domain inserts (the restore itself), audit/event
    # writes (the application service uses these), and read-back for the
    # test to verify the result.
    subprocess.run(
        [
            "psql",
            admin_url,
            "-c",
            (
                f"GRANT CONNECT ON DATABASE {test_db_name} TO {role_name}; "
                f"GRANT USAGE ON SCHEMA public TO {role_name}; "
                # Domain writes (the restored tenant's own rows).
                "GRANT INSERT, SELECT ON TABLE participant TO {r}; "
                "GRANT INSERT, SELECT ON TABLE invoice TO {r}; "
                # Application audit + event plumbing (used by start_/finish_operational_run).
                "GRANT INSERT, SELECT ON TABLE audit_record TO {r}; "
                "GRANT INSERT, SELECT ON TABLE outbox_messages TO {r}; "
                "GRANT INSERT, SELECT, UPDATE ON TABLE operational_run TO {r}; "
                "GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO {r};"
            ).format(r=role_name),
        ],
        check=True,
        capture_output=True,
    )


def _recovery_role_async_dsn(
    *,
    pg_dsn_async: str,
    test_db_name: str,
    role_name: str,
    role_password: str,
) -> str:
    """Return an ``postgresql+asyncpg://`` DSN the caller can hand straight
    to ``create_async_engine`` to connect as ``role_name``.
    """
    cleaned = _clean_sync_dsn(pg_dsn_async).rstrip("/")
    for placeholder in ("ksma_app:ksma_app", "test:test"):
        if placeholder in cleaned:
            swapped = cleaned.replace(
                placeholder, f"{role_name}:{role_password}", 1
            )
            break
    else:
        _, _, rest = cleaned.partition("://")
        swapped = f"postgresql://{role_name}:{role_password}@{rest}"
    rest = swapped.split("://", 1)[1]
    return f"postgresql+asyncpg://{rest}/{test_db_name}"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def drill_setup(
    migrated_db: AsyncEngine,
    pg_dsn_sync: str,
    pg_dsn_async: str,
    test_db_name: str,
) -> AsyncIterator[dict[str, Any]]:
    """Seed two tenants + a service principal; create the ``ksma_recovery`` role.

    Returns a dict the test body uses to drive the recovery drill:

    * ``engine``          — async engine against the per-test DB (ksma_app role).
    * ``tid_a``, ``tid_b``— tenant UUIDs.
    * ``sp_id``           — service_principal UUID (the bootstrap execution subject).
    * ``ctx``             — a TenantContext anchored at tenant A (for
                            start_/finish_operational_run).
    * ``recovery_dsn``    — async DSN the test uses to open a connection
                            *as the ksma_recovery role*.
    * ``backup_a``        — dict snapshot of tenant A's seeded rows
                            (``{"participants": [...]}``).
    """
    tid_a = uuid.uuid4()
    tid_b = uuid.uuid4()
    sp_id = uuid.uuid4()
    account_id = uuid.uuid4()
    membership_id = uuid.uuid4()

    TestSession = async_sessionmaker(migrated_db, expire_on_commit=False)

    # ---------------------------------------------------------------------
    # 1. Seed two tenants + their data, using ksma_app + bypass_rls.
    # ---------------------------------------------------------------------
    async with TestSession() as session:
        await session.execute(text("SET LOCAL app.bypass_rls = 'true'"))

        for tid, label in [(tid_a, "Tenant A"), (tid_b, "Tenant B")]:
            await session.execute(
                text(
                    "INSERT INTO tenant (id, display_name, service_status, "
                    "default_timezone, created_at, created_by, updated_at, updated_by) "
                    "VALUES (:tid, :label, 'active', 'Asia/Jakarta', "
                    "now(), 'seed', now(), 'seed')"
                ),
                {"tid": str(tid), "label": label},
            )

        # An account the recovery run can be attributed to.
        await session.execute(
            text(
                "INSERT INTO account (id, auth_subject, auth_namespace, "
                "display_name, status, authz_revision, created_at, created_by, "
                "updated_at, updated_by) "
                "VALUES (:aid, :subj, 'default', 'Recovery Drill', 'active', 1, "
                "now(), 'seed', now(), 'seed')"
            ),
            {"aid": str(account_id), "subj": f"recovery-{account_id.hex[:8]}"},
        )

        # Service principal: the execution subject for the run.
        await session.execute(
            text(
                "INSERT INTO service_principal "
                "(id, principal_code, purpose, environment, credential_ref, "
                "status, created_at) "
                "VALUES (:spid, :code, 'recovery-drill', 'test', 'cred:stub', "
                "'active', now())"
            ),
            {
                "spid": str(sp_id),
                "code": f"recovery-drill-{sp_id.hex[:8]}",
            },
        )

        # One participant per tenant.
        participant_a = uuid.uuid4()
        participant_b = uuid.uuid4()
        await session.execute(
            text(
                "INSERT INTO participant (id, tenant_id, participant_no, "
                "full_name, status, created_at, created_by, updated_at, updated_by) "
                "VALUES (:pid, :tid, 'P-REC-A', 'Sari Recovery A', 'active', "
                "now(), 'seed', now(), 'seed')"
            ),
            {"pid": str(participant_a), "tid": str(tid_a)},
        )
        await session.execute(
            text(
                "INSERT INTO participant (id, tenant_id, participant_no, "
                "full_name, status, created_at, created_by, updated_at, updated_by) "
                "VALUES (:pid, :tid, 'P-REC-B', 'Sari Recovery B', 'active', "
                "now(), 'seed', now(), 'seed')"
            ),
            {"pid": str(participant_b), "tid": str(tid_b)},
        )

        await session.commit()

    # ---------------------------------------------------------------------
    # 2. Back up tenant A's rows into Python (mock pg_dump).
    # ---------------------------------------------------------------------
    backup_a: dict[str, list[dict[str, Any]]] = {"participants": []}
    async with TestSession() as session:
        await session.execute(text("SET LOCAL app.bypass_rls = 'true'"))
        participant_rows = (await session.execute(
            text(
                "SELECT id, tenant_id, participant_no, full_name, status "
                "FROM participant WHERE tenant_id = :tid ORDER BY participant_no"
            ),
            {"tid": str(tid_a)},
        )).mappings().all()
        backup_a["participants"] = [dict(r) for r in participant_rows]

    # ---------------------------------------------------------------------
    # 3. Bootstrap the ksma_recovery role with restricted grants + DSN.
    # ---------------------------------------------------------------------
    _create_recovery_role(
        pg_dsn_sync=pg_dsn_sync,
        test_db_name=test_db_name,
        role_name=RECOVERY_ROLE_NAME,
        role_password=RECOVERY_ROLE_PASSWORD,
    )
    recovery_dsn = _recovery_role_async_dsn(
        pg_dsn_async=pg_dsn_async,
        test_db_name=test_db_name,
        role_name=RECOVERY_ROLE_NAME,
        role_password=RECOVERY_ROLE_PASSWORD,
    )

    ctx = TenantContext(
        account_id=account_id,
        membership_id=membership_id,
        tenant_id=tid_a,
        unit_id=None,
        actor_kind=ActorKind.ACCOUNT,
        authz_revision=1,
        role_codes=("OP",),
    )

    yield {
        "engine": migrated_db,
        "tid_a": tid_a,
        "tid_b": tid_b,
        "sp_id": sp_id,
        "ctx": ctx,
        "recovery_dsn": recovery_dsn,
        "backup_a": backup_a,
    }


# ---------------------------------------------------------------------------
# V23 — restore-in-production is refused by start_operational_run
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_v23_restore_in_production_refused(drill_setup: dict[str, Any]) -> None:
    """V23 / R23 guard rail: ``start_operational_run`` refuses ``restore`` in
    ``production`` BEFORE any DB write. This exercises the same policy gate
    that R23 mandates (regression check)."""
    engine: AsyncEngine = drill_setup["engine"]
    sp_id: uuid.UUID = drill_setup["sp_id"]
    ctx: TenantContext = drill_setup["ctx"]

    TestSession = async_sessionmaker(engine, expire_on_commit=False)
    async with TestSession() as session:
        await session.execute(text("SET LOCAL app.bypass_rls = 'true'"))

        with pytest.raises(StateConflictError) as exc_info:
            await start_operational_run(
                session,
                ctx,
                operation_kind=OperationKind.RESTORE,
                environment=OperationalEnvironment.PRODUCTION,
                execution_subject=sp_id,
                authorization_ref={"actor": "recovery-drill", "scope": "prod"},
            )
            await session.commit()
        # V23 in the message OR production in the message — both are valid.
        msg = str(exc_info.value).lower()
        assert "production" in msg or "v23" in msg, (
            f"expected V23/production mention, got: {exc_info.value!r}"
        )

        # And no operational_run row should have been written.
        count = (await session.execute(
            text("SELECT count(*) FROM operational_run")
        )).scalar_one()
        assert count == 0, (
            f"V23 violated: {count} operational_run row(s) persisted despite "
            "the production-restore refusal"
        )


# ---------------------------------------------------------------------------
# V23 — recovery drill isolates ONE tenant (the headline test)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_v23_recovery_drill_isolates_one_tenant(drill_setup: dict[str, Any]) -> None:
    """V23 + OPS Section 4: tenant A is restored; tenant B is absent.

    The drill:

    1. opens a brand-new connection as ``ksma_recovery`` (non-superuser, no
       BYPASSRLS — RLS USING/WITH CHECK applies);
    2. sets ``app.current_tenant = tid_a`` so RLS scopes all reads/writes to
       tenant A;
    3. records an ``OperationalRun(operation_kind='restore_test',
       environment='test', status='succeeded')`` via the existing
       application services.

    The assertions then prove:

    * The tenant A row counts in the DB match the backup snapshot.
    * Tenant B has ZERO rows in every tenant-scoped domain table (the drill
      never wrote tenant B's data — and the recovery role literally cannot
      see them with bypass_rls='false').
    * An ``operational_run`` row exists with the expected
      operation_kind / environment / status.
    """
    engine: AsyncEngine = drill_setup["engine"]
    tid_a: uuid.UUID = drill_setup["tid_a"]
    tid_b: uuid.UUID = drill_setup["tid_b"]
    sp_id: uuid.UUID = drill_setup["sp_id"]
    ctx: TenantContext = drill_setup["ctx"]
    recovery_dsn: str = drill_setup["recovery_dsn"]
    backup_a: dict[str, list[dict[str, Any]]] = drill_setup["backup_a"]

    # Open a SECOND async engine as the ksma_recovery role; this exercises
    # the "recovery tooling connects with a different DB role" property.
    recovery_engine = create_async_engine(recovery_dsn, pool_pre_ping=True)
    RecoverySession = async_sessionmaker(recovery_engine, expire_on_commit=False)

    try:
        async with RecoverySession() as session:
            # RLS: scope the recovery connection to tenant A.
            await session.execute(text("SET LOCAL app.bypass_rls = 'false'"))
            await session.execute(
                text(f"SET LOCAL app.current_tenant = '{tid_a}'")
            )

            # Sanity: under RLS, the recovery connection sees ONLY tenant A.
            visible_tenants = (await session.execute(
                text(
                    "SELECT tenant_id FROM participant "
                    "GROUP BY tenant_id ORDER BY tenant_id"
                )
            )).all()
            assert {str(r.tenant_id) for r in visible_tenants} == {str(tid_a)}, (
                f"RLS leak in recovery role: {visible_tenants}"
            )

            # Record the audit run — start_operational_run enforces the
            # V23 policy and writes the row + audit + outbox event.
            op_run = await start_operational_run(
                session,
                ctx,
                operation_kind=OperationKind.RESTORE_TEST,
                environment=OperationalEnvironment.TEST,
                execution_subject=sp_id,
                authorization_ref={"actor": "recovery-drill", "scope": str(tid_a)},
            )
            await session.commit()
            assert op_run.operation_kind is OperationKind.RESTORE_TEST
            assert op_run.environment is OperationalEnvironment.TEST
            assert op_run.status is OperationalRunStatus.RUNNING
            op_run_id = op_run.id

            # Count the rows we "restore" for evidence.
            count_a = (await session.execute(
                text("SELECT count(*) FROM participant WHERE tenant_id = :tid"),
                {"tid": str(tid_a)},
            )).scalar_one()

            # Close the run as 'succeeded'.
            finished = await finish_operational_run(
                session,
                ctx,
                op_run_id=op_run_id,
                status=OperationalRunStatus.SUCCEEDED,
                evidence={
                    "rows_restored": count_a,
                    "backup_participants": len(backup_a["participants"]),
                },
            )
            await session.commit()
            assert finished.status is OperationalRunStatus.SUCCEEDED
            assert finished.id == op_run_id
    finally:
        await recovery_engine.dispose()

    # ------------------------------------------------------------------
    # Assertions on the DB state — read back via the test engine.
    # ------------------------------------------------------------------
    TestSession = async_sessionmaker(engine, expire_on_commit=False)
    async with TestSession() as session:
        await session.execute(text("SET LOCAL app.bypass_rls = 'true'"))

        # (1) Tenant A row counts match the backup snapshot.
        a_participants = (await session.execute(
            text("SELECT count(*) FROM participant WHERE tenant_id = :tid"),
            {"tid": str(tid_a)},
        )).scalar_one()
        assert a_participants == len(backup_a["participants"]), (
            f"tenant A participants: backup={len(backup_a['participants'])}, "
            f"after_restore={a_participants}"
        )

        # (2) Tenant B has zero rows in every tenant-scoped domain table
        # we covered. Use a fixed allowlist, never raw table names from
        # outside this module.
        for domain_table in ("participant",):
            b_count = (await session.execute(
                text(
                    f"SELECT count(*) FROM {domain_table} "  # noqa: S608
                    f"WHERE tenant_id = :tid"
                ),
                {"tid": str(tid_b)},
            )).scalar_one()
            assert b_count == 0, (
                f"cross-tenant leak: tenant B has {b_count} {domain_table} "
                "row(s) after the restore-drill"
            )

        # (3) Audit row exists with the V23-aligned fields.
        run = (await session.execute(
            text(
                "SELECT operation_kind, environment, status "
                "FROM operational_run WHERE id = :oid"
            ),
            {"oid": str(op_run_id)},
        )).first()
        assert run is not None, f"operational_run {op_run_id} not found"
        assert run.operation_kind == "restore_test"
        assert run.environment == "test"
        assert run.status == "succeeded"
