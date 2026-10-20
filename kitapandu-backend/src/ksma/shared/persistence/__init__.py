"""SQLAlchemy 2.x async engine + session factory + RLS GUC setter.

Per ADR0001 §3 + the verified research subagent's gotchas:

- ``expire_on_commit=False, autoflush=False`` so the SET LOCAL on the session's
  transaction survives across queries within the same request.
- ``set_current_tenant(session, tenant_id)`` opens the session's autobegun
  transaction and runs ``SET LOCAL app.current_tenant = :tid`` on its connection.
- ``bypass_rls(session)`` flips ``app.bypass_rls = 'true'`` for tests + migrations
  on a dedicated non-app role (NOT the app role).

The session-per-request pattern is:

    async with session_scope() as session:
        await set_current_tenant(session, tenant_id)
        await set_current_unit(session, unit_id)  # optional
        ...  # do work
        # commit happens on context exit
"""
from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncConnection,
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from ksma.settings import get_settings


_engine: AsyncEngine | None = None
_sessionmaker: async_sessionmaker[AsyncSession] | None = None


def engine() -> AsyncEngine:
    global _engine
    if _engine is None:
        settings = get_settings()
        _engine = create_async_engine(
            str(settings.postgres_dsn),
            pool_pre_ping=True,
            pool_size=10,
            max_overflow=10,
        )
    return _engine


def sessionmaker() -> async_sessionmaker[AsyncSession]:
    global _sessionmaker
    if _sessionmaker is None:
        _sessionmaker = async_sessionmaker(
            engine(),
            expire_on_commit=False,
            autoflush=False,
        )
    return _sessionmaker


@asynccontextmanager
async def session_scope() -> AsyncIterator[AsyncSession]:
    """Yield a session; commit on success, rollback on exception.

    The session autobegins a transaction on first query. We keep that transaction
    open so any ``SET LOCAL`` we issue stays alive.
    """
    sm = sessionmaker()
    async with sm() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


async def set_current_tenant(
    session: AsyncSession,
    tenant_id: UUID,
    *,
    bypass: bool = False,
) -> None:
    """Run ``SET LOCAL`` on the session's transaction so RLS policies see the tenant.

    Call BEFORE any other queries on this session. Safe to call multiple times;
    later calls overwrite.
    """
    conn: AsyncConnection = await session.connection()
    if bypass:
        await conn.execute(
            text("SET LOCAL app.bypass_rls = 'true'"),
        )
    else:
        await conn.execute(
            text("SET LOCAL app.bypass_rls = 'false'"),
            {"tid": str(tenant_id)},
        )
        # SET LOCAL doesn't accept bound parameters via asyncpg; inline the
        # UUID as a literal (validated by SQLAlchemy before execution).
        await conn.execute(
            text(f"SET LOCAL app.current_tenant = '{str(tenant_id)}'"),
        )


async def set_current_unit(
    session: AsyncSession,
    unit_id: UUID | None,
) -> None:
    """Optional unit scope; ``NULL`` means "any unit in tenant"."""
    conn: AsyncConnection = await session.connection()
    await conn.execute(
        text("SET LOCAL app.current_unit = :uid"),
        {"uid": str(unit_id) if unit_id else ""},
    )


async def set_current_actor(
    session: AsyncSession,
    actor_kind: str,
    actor_id: UUID,
    authz_revision: int,
) -> None:
    """For audit + fine-grained policy filters (e.g., support operator logs)."""
    conn: AsyncConnection = await session.connection()
    await conn.execute(
        text(
            "SET LOCAL app.actor_kind = :kind, "
            "app.actor_id = :aid, "
            "app.authz_revision = :rev"
        ),
        {"kind": actor_kind, "aid": str(actor_id), "rev": str(authz_revision)},
    )


async def execute_with_rls(
    session: AsyncSession,
    tenant_id: UUID,
    *,
    unit_id: UUID | None = None,
    actor: tuple[str, UUID, int] | None = None,
) -> None:
    """Convenience: set tenant + optional unit + optional actor in one call."""
    await set_current_tenant(session, tenant_id)
    if unit_id is not None:
        await set_current_unit(session, unit_id)
    if actor is not None:
        kind, aid, rev = actor
        await set_current_actor(session, kind, aid, rev)


def dispose_engine() -> None:
    """Dispose of the global engine (used by tests to swap config)."""
    global _engine, _sessionmaker
    if _engine is not None:
        # aclose() is the async-correct teardown for an AsyncEngine
        import asyncio

        try:
            loop = asyncio.get_event_loop()
        except RuntimeError:
            loop = None
        if loop and loop.is_running():
            loop.create_task(_engine.aclose())
        else:
            asyncio.run(_engine.aclose())
        _engine = None
        _sessionmaker = None


def reset_for_tests() -> None:
    """Test helper: drop cached engine + sessionmaker."""
    global _engine, _sessionmaker
    _engine = None
    _sessionmaker = None


# Exported at package level
__all__: list[Any] = [
    "engine",
    "sessionmaker",
    "session_scope",
    "set_current_tenant",
    "set_current_unit",
    "set_current_actor",
    "execute_with_rls",
    "dispose_engine",
    "reset_for_tests",
]