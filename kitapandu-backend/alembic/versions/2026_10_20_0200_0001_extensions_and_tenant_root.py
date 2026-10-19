"""Extensions + RLS infrastructure

Revision ID: 0001
Revises:
Create Date: 2026-10-20 02:00

Enables PostgreSQL extensions required by later migrations and creates the
GUC infrastructure used by RLS policies.

- btree_gist: needed for `EXCLUDE USING gist` with mixed `=` + range overlap (R07).
- pgcrypto: gen_random_uuid() for ID columns.
- citext (optional, not enabled here — we lowercase in app).

GUCs:
- app.current_tenant (uuid): set by `set_current_tenant()` per request.
- app.current_unit (text): optional, set by `set_current_unit()`.
- app.bypass_rls (text 'true'|'false'): bypass for migrations + tests.
- app.actor_kind, app.actor_id, app.authz_revision: for audit / finer policies.

Root tables (Tenant) come in migration 0002; this migration is purely infra.
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op

revision: str = "0001"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Required extensions
    op.execute("CREATE EXTENSION IF NOT EXISTS pgcrypto")
    op.execute("CREATE EXTENSION IF NOT EXISTS btree_gist")

    # Helper: a default UUID generator kept here so the same name is referenced
    # everywhere in DDL. gen_random_uuid() ships with pgcrypto on PG 13+.
    op.execute(
        """
        CREATE OR REPLACE FUNCTION ksma_uuid() RETURNS uuid
        LANGUAGE sql VOLATILE PARALLEL SAFE AS $$
          SELECT gen_random_uuid()
        $$
        """
    )

    # Convenience: a function that reads the current tenant GUC and casts to uuid,
    # returning NULL if not set. Used inside RLS policies so we can use `IS NULL`
    # semantics for "bypass not engaged, tenant not set" → deny.
    op.execute(
        """
        CREATE OR REPLACE FUNCTION ksma_current_tenant() RETURNS uuid
        LANGUAGE sql STABLE AS $$
          SELECT NULLIF(current_setting('app.current_tenant', true), '')::uuid
        $$
        """
    )
    op.execute(
        """
        CREATE OR REPLACE FUNCTION ksma_bypass_rls() RETURNS boolean
        LANGUAGE sql STABLE AS $$
          SELECT coalesce(current_setting('app.bypass_rls', true), 'false') = 'true'
        $$
        """
    )


def downgrade() -> None:
    op.execute("DROP FUNCTION IF EXISTS ksma_bypass_rls()")
    op.execute("DROP FUNCTION IF EXISTS ksma_current_tenant()")
    op.execute("DROP FUNCTION IF EXISTS ksma_uuid()")
    op.execute("DROP EXTENSION IF EXISTS btree_gist")
    op.execute("DROP EXTENSION IF EXISTS pgcrypto")