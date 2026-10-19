"""Participant context — Participant, RelatedParty, ParticipantAccountLink, ParticipantRelation

Revision ID: 0003
Revises: 0002
Create Date: 2026-10-20 02:10

Creates the participant context tables per KSMA Data Dictionary §3.9–§3.12 with
tenant RLS. The encrypted ``support_needs_note`` is stored as bytea from
pgp_sym_encrypt() with a per-tenant key GUC (``app.tenant_pii_key``).
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
from sqlalchemy import text

revision: str = "0003"
down_revision: Union[str, None] = "0002"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # participant (T scope)
    op.execute(
        """
        CREATE TABLE participant (
            id                      uuid PRIMARY KEY DEFAULT ksma_uuid(),
            tenant_id               uuid NOT NULL REFERENCES tenant(id) ON DELETE RESTRICT,
            participant_no          text NOT NULL,
            full_name               text NOT NULL,
            birth_date              date,
            contact                 jsonb,
            identity_reference      jsonb,
            status                  text NOT NULL CHECK (status IN ('active','inactive','archived')),
            support_needs_note_enc  bytea,
            created_at              timestamptz NOT NULL DEFAULT now(),
            created_by              text NOT NULL,
            updated_at              timestamptz NOT NULL DEFAULT now(),
            updated_by              text NOT NULL,
            UNIQUE (tenant_id, participant_no),
            CHECK (birth_date IS NULL OR birth_date <= CURRENT_DATE)
        )
        """
    )
    op.execute("CREATE INDEX ix_participant_tenant ON participant(tenant_id)")
    op.execute("CREATE INDEX ix_participant_name ON participant(tenant_id, LOWER(full_name))")

    # related_party (T scope)
    op.execute(
        """
        CREATE TABLE related_party (
            id                  uuid PRIMARY KEY DEFAULT ksma_uuid(),
            tenant_id           uuid NOT NULL REFERENCES tenant(id) ON DELETE RESTRICT,
            party_type          text NOT NULL CHECK (party_type IN ('person','organization')),
            display_name        text NOT NULL,
            contact             jsonb,
            organization_ref    text,
            status              text NOT NULL CHECK (status IN ('active','inactive')),
            created_at          timestamptz NOT NULL DEFAULT now(),
            created_by          text NOT NULL,
            updated_at          timestamptz NOT NULL DEFAULT now(),
            updated_by          text NOT NULL
        )
        """
    )
    op.execute("CREATE INDEX ix_related_party_tenant ON related_party(tenant_id)")
    op.execute("CREATE INDEX ix_related_party_name ON related_party(tenant_id, LOWER(display_name))")

    # participant_account_link (T scope)
    op.execute(
        """
        CREATE TABLE participant_account_link (
            id                  uuid PRIMARY KEY DEFAULT ksma_uuid(),
            tenant_id           uuid NOT NULL REFERENCES tenant(id) ON DELETE RESTRICT,
            account_id          uuid NOT NULL REFERENCES account(id) ON DELETE RESTRICT,
            participant_id      uuid REFERENCES participant(id),
            related_party_id    uuid REFERENCES related_party(id),
            verification        jsonb NOT NULL,
            valid_from          timestamptz NOT NULL DEFAULT now(),
            valid_until         timestamptz,
            status              text NOT NULL CHECK (status IN ('pending','active','revoked')),
            created_at          timestamptz NOT NULL DEFAULT now(),
            created_by          text NOT NULL,
            updated_at          timestamptz NOT NULL DEFAULT now(),
            updated_by          text NOT NULL,
            CHECK (
              (participant_id IS NOT NULL AND related_party_id IS NULL) OR
              (participant_id IS NULL AND related_party_id IS NOT NULL)
            ),
            UNIQUE (tenant_id, account_id, participant_id, related_party_id)
        )
        """
    )
    op.execute("CREATE INDEX ix_link_account ON participant_account_link(account_id)")
    op.execute("CREATE INDEX ix_link_participant ON participant_account_link(participant_id)")

    # participant_relation (T scope)
    op.execute(
        """
        CREATE TABLE participant_relation (
            id                  uuid PRIMARY KEY DEFAULT ksma_uuid(),
            tenant_id           uuid NOT NULL REFERENCES tenant(id) ON DELETE RESTRICT,
            participant_id      uuid NOT NULL REFERENCES participant(id) ON DELETE RESTRICT,
            related_party_id    uuid NOT NULL REFERENCES related_party(id) ON DELETE RESTRICT,
            relation_kind       text NOT NULL CHECK (relation_kind IN
                                  ('academic_guardian','payer','sponsor')),
            financial_scope     jsonb NOT NULL DEFAULT '{}'::jsonb,
            verification        jsonb NOT NULL,
            valid_from          timestamptz NOT NULL DEFAULT now(),
            valid_until         timestamptz,
            status              text NOT NULL CHECK (status IN ('pending','active','expired','revoked')),
            created_at          timestamptz NOT NULL DEFAULT now(),
            created_by          text NOT NULL,
            updated_at          timestamptz NOT NULL DEFAULT now(),
            updated_by          text NOT NULL,
            UNIQUE (tenant_id, participant_id, related_party_id, relation_kind)
        )
        """
    )
    op.execute(
        "CREATE INDEX ix_relation_participant ON participant_relation(participant_id)"
    )
    op.execute(
        "CREATE INDEX ix_relation_related_party ON participant_relation(related_party_id)"
    )

    # RLS — every T/U table.
    rls_tables = ["participant", "related_party", "participant_account_link", "participant_relation"]
    for tbl in rls_tables:
        op.execute(f"ALTER TABLE {tbl} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {tbl} FORCE ROW LEVEL SECURITY")
        op.execute(
            f"""
            CREATE POLICY {tbl}_tenant_isolation ON {tbl}
            USING (
              ksma_bypass_rls()
              OR tenant_id = ksma_current_tenant()
            )
            WITH CHECK (
              ksma_bypass_rls()
              OR tenant_id = ksma_current_tenant()
            )
            """
        )


def downgrade() -> None:
    rls_tables = ["participant_relation", "participant_account_link", "related_party", "participant"]
    for tbl in rls_tables:
        op.execute(f"DROP POLICY IF EXISTS {tbl}_tenant_isolation ON {tbl}")
        op.execute(f"ALTER TABLE {tbl} DISABLE ROW LEVEL SECURITY")
    op.execute("DROP TABLE IF EXISTS participant_relation CASCADE")
    op.execute("DROP TABLE IF EXISTS participant_account_link CASCADE")
    op.execute("DROP TABLE IF EXISTS related_party CASCADE")
    op.execute("DROP TABLE IF EXISTS participant CASCADE")