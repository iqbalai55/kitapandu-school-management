"""Integration test fixtures: ephemeral Postgres 16 via testcontainers.

Per ADR-0001 + ADR-0002 + ticket P1: every integration test must run against
a real Postgres to validate RLS, FOR UPDATE locks, and EXCLUDE constraints.

Strategy:
- One session-scoped Postgres container (started once for the suite).
- Each test gets a per-test database (created from a template) for isolation.
- Per-test alembic upgrade head runs against the fresh DB.
- ``reset_for_tests()`` clears the global engine so each test gets a fresh one
  pointing at its test database.
"""
from __future__ import annotations

import asyncio
import os
import subprocess
import uuid
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from testcontainers.postgres import PostgresContainer


def _clean_sync_dsn(url: str) -> str:
    """Strip any SQLAlchemy dialect prefix so psql + psycopg accept it."""
    return (url
            .replace("postgresql+psycopg2://", "postgresql://", 1)
            .replace("postgresql+asyncpg://", "postgresql://", 1))


def _clean_async_dsn(url: str) -> str:
    """Ensure asyncpg dialect for SQLAlchemy async engine."""
    if url.startswith("postgresql+asyncpg://"):
        return url
    if url.startswith("postgresql+psycopg2://"):
        return url.replace("postgresql+psycopg2://", "postgresql+asyncpg://", 1)
    if url.startswith("postgresql://"):
        return url.replace("postgresql://", "postgresql+asyncpg://", 1)
    return url

from ksma.shared.persistence import reset_for_tests, set_current_tenant


# Session-scoped container (started once for the whole test session).
@pytest.fixture(scope="session")
def pg_container() -> PostgresContainer:
    container = PostgresContainer("postgres:16-alpine")
    container.start()
    # The default test user is a SUPERUSER; superusers bypass RLS even when
    # FORCE ROW LEVEL SECURITY is set. Create a non-superuser ``ksma_app``
    # role that the application will connect as so the RLS policy is
    # actually enforced end-to-end.
    import subprocess
    admin_url = _clean_sync_dsn(container.get_connection_url())
    subprocess.run(
        ["psql", admin_url, "-c",
         "DO $$ BEGIN "
         "IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'ksma_app') THEN "
         "CREATE ROLE ksma_app WITH LOGIN PASSWORD 'ksma_app' NOSUPERUSER; "
         "END IF; "
         "END $$;"],
        check=True, capture_output=True,
    )
    return container


@pytest.fixture(autouse=True)
def bootstrap_non_superuser(pg_dsn_sync: str, pg_container: PostgresContainer) -> None:
    """Create the ksma_app role inside the container if it doesn't exist.

    Alembic needs a superuser (the default test user) for CREATE EXTENSION and
    bootstrap migrations. The application connects as ksma_app (a non-superuser)
    so RLS is enforced end-to-end per FORCE ROW LEVEL SECURITY. Superusers
    bypass RLS even with FORCE.
    """
    import subprocess
    admin_url = _clean_sync_dsn(pg_dsn_sync)
    # CREATE ROLE if not exists.
    subprocess.run(
        ["psql", admin_url, "-c",
         "DO $$ BEGIN "
         "IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'ksma_app') THEN "
         "CREATE ROLE ksma_app WITH LOGIN PASSWORD 'ksma_app'; "
         "END IF; "
         "END $$;"],
        check=False, capture_output=True,
    )
    # Grant DML on the default DB; per-DB grants happen in the per-test
    # create_test_database fixture once we know the test DB name.


@pytest.fixture(scope="session")
def pg_dsn_sync(pg_container: PostgresContainer) -> str:
    """Sync DSN (psycopg) used by alembic (running as the postgres superuser)."""
    # alembic needs the superuser to create extensions and seed.
    url = pg_container.get_connection_url()
    return url


@pytest.fixture(scope="session")
def pg_dsn_async(pg_container: PostgresContainer) -> str:
    """Async DSN (asyncpg) used by the application (non-superuser)."""
    # Override credentials so the application connects as `ksma_app`
    # (NOSUPERUSER) and RLS is enforced.
    url = pg_container.get_connection_url()
    base = url.replace("postgresql+psycopg2://", "postgresql+asyncpg://", 1)
    return base.replace("test:test@", "ksma_app:ksma_app@", 1)


@pytest.fixture
def test_db_name() -> str:
    return f"ksma_test_{uuid.uuid4().hex[:10]}"


@pytest.fixture(autouse=True)
def create_test_database(pg_dsn_sync: str, test_db_name: str) -> None:
    """Create the per-test database, then drop it on teardown."""
    # psql wants a plain postgresql:// URL; strip the SQLAlchemy dialect prefix.
    admin_url = _clean_sync_dsn(pg_dsn_sync).rsplit("/", 1)[0] + "/postgres"
    subprocess.run(
        ["psql", admin_url, "-c", f"CREATE DATABASE {test_db_name};"],
        check=True, capture_output=True,
    )
    yield
    # Force-disconnect any active sessions before dropping (RLS contexts hold
    # connections open).
    subprocess.run(
        ["psql", admin_url, "-c",
         f"SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = '{test_db_name}';"],
        check=False, capture_output=True,
    )
    subprocess.run(
        ["psql", admin_url, "-c", f"DROP DATABASE {test_db_name};"],
        check=False, capture_output=True,
    )


@pytest.fixture(autouse=True)
def grant_app_role_on_test_db(pg_dsn_sync: str, test_db_name: str) -> None:
    """Grant ksma_app table DML on the test DB. Runs AFTER alembic creates the
    schema, so the GRANT statements apply to existing tables."""
    import subprocess
    admin_url = _clean_sync_dsn(pg_dsn_sync)
    test_url = admin_url.rsplit("/", 1)[0] + "/" + test_db_name
    subprocess.run(
        ["psql", test_url, "-c",
         f"GRANT CONNECT ON DATABASE {test_db_name} TO ksma_app; "
         "GRANT USAGE ON SCHEMA public TO ksma_app; "
         "GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO ksma_app; "
         "GRANT USAGE, SELECT, UPDATE ON ALL SEQUENCES IN SCHEMA public TO ksma_app; "
         "ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT, INSERT, UPDATE, DELETE "
         "  ON TABLES TO ksma_app;"],
        check=False, capture_output=True,
    )
    yield


@pytest_asyncio.fixture
async def migrated_db(pg_dsn_async: str, test_db_name: str, pg_dsn_sync: str) -> AsyncIterator[AsyncEngine]:
    """Per-test: run alembic upgrade head on the test database, return engine."""
    # Point alembic at the test DB by setting the sync DSN env var.
    test_sync_url = _clean_sync_dsn(pg_dsn_sync).rsplit("/", 1)[0] + "/" + test_db_name
    test_async_url = pg_dsn_async.rsplit("/", 1)[0] + "/" + test_db_name
    env = os.environ.copy()
    env["KITAPANDU_POSTGRES_DSN_SYNC"] = test_sync_url
    env["KITAPANDU_POSTGRES_DSN"] = test_async_url
    # CRITICAL: clear both the persistence module's cached engine AND the
    # pydantic-settings cached Settings, otherwise the next call to
    # ``session_scope`` would use the dev DB DSN (``ksma:ksma@localhost``).
    from ksma.shared.persistence import reset_for_tests
    reset_for_tests()
    from ksma.settings import get_settings
    get_settings.cache_clear()
    # Also override the env so pydantic-settings rebuilds the cached Settings
    # pointing at the test DB.
    os.environ["KITAPANDU_POSTGRES_DSN_SYNC"] = test_sync_url
    os.environ["KITAPANDU_POSTGRES_DSN"] = test_async_url
    project_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    subprocess.run(
        ["alembic", "upgrade", "head"],
        cwd=project_root, env=env, check=True, capture_output=True,
    )
    # Build the async engine against the test DB.
    engine = create_async_engine(test_async_url, pool_pre_ping=True)
    yield engine
    await engine.dispose()


@pytest_asyncio.fixture
async def tenant_session(migrated_db: AsyncEngine, pg_dsn_async: str, test_db_name: str, pg_dsn_sync: str):
    """Yield an async session with RLS context for tenant A."""
    tenant_a = uuid.uuid4()
    tenant_b = uuid.uuid4()
    # The application engine (migrated_db) connects as ksma_app (non-superuser)
    # so RLS is enforced end-to-end. To seed data we use a SUPERUSER engine
    # that bypasses RLS via the FORCE bypass (SET LOCAL app.bypass_rls = 'true').
    # We give the superuser the ability to set this GUC by revoking + granting
    # the necessary privileges on the ksma_uuid() function and tables.
    superuser_async_url = _clean_async_dsn(pg_dsn_async).replace(
        "ksma_app:ksma_app@", "test:test@", 1
    )
    # Append test DB name to the URL (the pg_dsn_async URL has no DB).
    superuser_async_url = superuser_async_url.rsplit("/", 1)[0] + "/" + test_db_name
    super_engine = create_async_engine(superuser_async_url, pool_pre_ping=True)
    try:
        async with super_engine.begin() as conn:
            # Bootstrap the GUC helpers' functions; they live in migration 0001.
            # The alembic upgrade already ran them.
            # Insert a fixture row in each tenant so we can assert RLS.
            await conn.execute(
                text(
                    "INSERT INTO tenant (id, display_name, service_status, default_timezone, "
                    "created_at, created_by, updated_at, updated_by) "
                    "VALUES (:tid, 'Tenant A', 'active', 'Asia/Jakarta', now(), 'seed', now(), 'seed')"
                ),
                {"tid": str(tenant_a)},
            )
            await conn.execute(
                text(
                    "INSERT INTO tenant (id, display_name, service_status, default_timezone, "
                    "created_at, created_by, updated_at, updated_by) "
                    "VALUES (:tid, 'Tenant B', 'active', 'Asia/Jakarta', now(), 'seed', now(), 'seed')"
                ),
                {"tid": str(tenant_b)},
            )
            # Insert one participant per tenant in the global U scope (participants
            # are tenant-scoped per the data dictionary).
            await conn.execute(
                text(
                    "INSERT INTO participant (id, tenant_id, participant_no, full_name, status, "
                    "created_at, created_by, updated_at, updated_by) "
                    "VALUES (ksma_uuid(), :tid, 'P-A-0001', 'Sari Tenant A', 'active', "
                    "now(), 'seed', now(), 'seed')"
                ),
                {"tid": str(tenant_a)},
            )
            await conn.execute(
                text(
                    "INSERT INTO participant (id, tenant_id, participant_no, full_name, status, "
                    "created_at, created_by, updated_at, updated_by) "
                    "VALUES (ksma_uuid(), :tid, 'P-B-0001', 'Sari Tenant B', 'active', "
                    "now(), 'seed', now(), 'seed')"
                ),
                {"tid": str(tenant_b)},
            )
            await conn.commit()
    finally:
        await super_engine.dispose()

    # Hand the caller two tenant ids + a session factory built from the
    # test engine directly. (We bypass ``ksma.shared.persistence.session_scope``
    # here because that helper builds its own engine from the cached settings,
    # which still points at the dev DB.)
    from sqlalchemy.ext.asyncio import async_sessionmaker
    TestSession = async_sessionmaker(migrated_db, expire_on_commit=False)

    async def _session_with_tenant(tid: uuid.UUID):
        # Don't use session.begin() — asyncpg autobegins on first query and
        # SET LOCAL sticks to that autobegun transaction. We just run the SET
        # then the read in sequence.
        async with TestSession() as session:
            tid_literal = str(tid)
            await session.execute(
                text("SET LOCAL app.bypass_rls = 'false'")
            )
            await session.execute(
                text(f"SET LOCAL app.current_tenant = '{tid_literal}'")
            )
            yield session

    yield {
        "engine": migrated_db,
        "tenant_a": tenant_a,
        "tenant_b": tenant_b,
        "session_for": _session_with_tenant,
    }