"""Program context — Program, ProgramVersion, AcademicPeriod, RubricVersion, RubricIndicator

Revision ID: 0004
Revises: 0003
Create Date: 2026-10-20 02:15

Creates the program tables per KSMA Data Dictionary §3.13–§3.17, §3.26–§3.27.
One of the two Unit-scoped program tables (U scope).
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op

revision: str = "0004"
down_revision: Union[str, None] = "0003"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # program (U scope)
    op.execute(
        """
        CREATE TABLE program (
            id                  uuid PRIMARY KEY DEFAULT ksma_uuid(),
            tenant_id           uuid NOT NULL REFERENCES tenant(id) ON DELETE RESTRICT,
            unit_id             uuid NOT NULL REFERENCES education_unit(id) ON DELETE RESTRICT,
            program_code        text NOT NULL,
            name                text NOT NULL,
            service_group       text NOT NULL,
            status              text NOT NULL CHECK (status IN ('draft','active','inactive')),
            current_version_id  uuid,
            created_at          timestamptz NOT NULL DEFAULT now(),
            created_by          text NOT NULL,
            updated_at          timestamptz NOT NULL DEFAULT now(),
            updated_by          text NOT NULL,
            UNIQUE (unit_id, program_code)
        )
        """
    )
    op.execute("CREATE INDEX ix_program_tenant ON program(tenant_id)")
    op.execute("CREATE INDEX ix_program_unit ON program(unit_id)")

    # program_version (U scope)
    op.execute(
        """
        CREATE TABLE program_version (
            id                  uuid PRIMARY KEY DEFAULT ksma_uuid(),
            tenant_id           uuid NOT NULL REFERENCES tenant(id) ON DELETE RESTRICT,
            unit_id             uuid NOT NULL REFERENCES education_unit(id) ON DELETE RESTRICT,
            program_id          uuid NOT NULL REFERENCES program(id) ON DELETE RESTRICT,
            version_no          integer NOT NULL CHECK (version_no > 0),
            learning_objectives text NOT NULL,
            curriculum          jsonb NOT NULL,
            effective_from      timestamptz,
            effective_until     timestamptz,
            status              text NOT NULL CHECK (status IN ('draft','published','retired')),
            created_at          timestamptz NOT NULL DEFAULT now(),
            created_by          text NOT NULL,
            updated_at          timestamptz NOT NULL DEFAULT now(),
            updated_by          text NOT NULL,
            UNIQUE (program_id, version_no),
            CHECK (effective_until IS NULL OR effective_until >= effective_from)
        )
        """
    )
    op.execute(
        "CREATE INDEX ix_program_version_program ON program_version(program_id)"
    )

    # Wire the program.current_version_id FK now that program_version exists.
    op.execute(
        "ALTER TABLE program ADD CONSTRAINT program_current_version_fk "
        "FOREIGN KEY (current_version_id) REFERENCES program_version(id)"
    )

    # academic_period (U scope)
    op.execute(
        """
        CREATE TABLE academic_period (
            id                  uuid PRIMARY KEY DEFAULT ksma_uuid(),
            tenant_id           uuid NOT NULL REFERENCES tenant(id) ON DELETE RESTRICT,
            unit_id             uuid NOT NULL REFERENCES education_unit(id) ON DELETE RESTRICT,
            period_code         text NOT NULL,
            name                text NOT NULL,
            period_kind         text NOT NULL CHECK (period_kind IN
                                  ('course_window','academic_year','semester')),
            start_date          timestamptz NOT NULL,
            end_date            timestamptz NOT NULL,
            status              text NOT NULL CHECK (status IN ('planned','active','closed')),
            created_at          timestamptz NOT NULL DEFAULT now(),
            created_by          text NOT NULL,
            updated_at          timestamptz NOT NULL DEFAULT now(),
            updated_by          text NOT NULL,
            UNIQUE (unit_id, period_code),
            CHECK (end_date >= start_date)
        )
        """
    )
    op.execute("CREATE INDEX ix_academic_period_unit ON academic_period(unit_id)")

    # rubric_version (U scope) — tied to program_version per ADR05 / §3.26
    op.execute(
        """
        CREATE TABLE rubric_version (
            id                  uuid PRIMARY KEY DEFAULT ksma_uuid(),
            tenant_id           uuid NOT NULL REFERENCES tenant(id) ON DELETE RESTRICT,
            unit_id             uuid NOT NULL REFERENCES education_unit(id) ON DELETE RESTRICT,
            program_version_id  uuid NOT NULL REFERENCES program_version(id) ON DELETE RESTRICT,
            version_no          integer NOT NULL CHECK (version_no > 0),
            name                text NOT NULL,
            status              text NOT NULL CHECK (status IN ('draft','published','retired')),
            published_at        timestamptz,
            approval            jsonb,
            created_at          timestamptz NOT NULL DEFAULT now(),
            created_by          text NOT NULL,
            updated_at          timestamptz NOT NULL DEFAULT now(),
            updated_by          text NOT NULL,
            UNIQUE (program_version_id, version_no)
        )
        """
    )
    op.execute(
        "CREATE INDEX ix_rubric_version_pv ON rubric_version(program_version_id)"
    )

    # rubric_indicator (U scope)
    op.execute(
        """
        CREATE TABLE rubric_indicator (
            id                  uuid PRIMARY KEY DEFAULT ksma_uuid(),
            tenant_id           uuid NOT NULL REFERENCES tenant(id) ON DELETE RESTRICT,
            rubric_version_id   uuid NOT NULL REFERENCES rubric_version(id) ON DELETE RESTRICT,
            indicator_code      text NOT NULL,
            label               text NOT NULL,
            value_kind          text NOT NULL CHECK (value_kind IN
                                  ('numeric','categorical','narrative')),
            scale               jsonb NOT NULL,
            weight              numeric,
            is_required         boolean NOT NULL DEFAULT true,
            created_at          timestamptz NOT NULL DEFAULT now(),
            created_by          text NOT NULL,
            updated_at          timestamptz NOT NULL DEFAULT now(),
            updated_by          text NOT NULL,
            UNIQUE (rubric_version_id, indicator_code)
        )
        """
    )
    op.execute(
        "CREATE INDEX ix_rubric_indicator_rv ON rubric_indicator(rubric_version_id)"
    )

    # RLS for each U table.
    rls_tables = ["program", "program_version", "academic_period", "rubric_version", "rubric_indicator"]
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
    rls_tables = ["rubric_indicator", "rubric_version", "academic_period", "program_version", "program"]
    for tbl in rls_tables:
        op.execute(f"DROP POLICY IF EXISTS {tbl}_tenant_isolation ON {tbl}")
        op.execute(f"ALTER TABLE {tbl} DISABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE program DROP CONSTRAINT IF EXISTS program_current_version_fk")
    op.execute("DROP TABLE IF EXISTS rubric_indicator CASCADE")
    op.execute("DROP TABLE IF EXISTS rubric_version CASCADE")
    op.execute("DROP TABLE IF EXISTS academic_period CASCADE")
    op.execute("DROP TABLE IF EXISTS program_version CASCADE")
    op.execute("DROP TABLE IF EXISTS program CASCADE")