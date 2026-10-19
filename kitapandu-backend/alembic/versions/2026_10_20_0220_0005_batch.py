"""Batch context — Batch, Application, Enrollment, EnrollmentChange, TeachingAssignment

Revision ID: 0005
Revises: 0004
Create Date: 2026-10-20 02:20

Creates the batch tables per KSMA Data Dictionary §3.16–§3.20 with the R7
exclusion constraint on ``enrollment.effective_range`` (R07, V06). The constraint
is the primary enforcement mechanism for "no overlapping active enrollment on
the same (participant, batch)" — the app layer's SELECT FOR UPDATE + count is
a belt-and-braces race guard, this constraint is the safety net.
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op

revision: str = "0005"
down_revision: Union[str, None] = "0004"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # batch (U scope)
    op.execute(
        """
        CREATE TABLE batch (
            id                  uuid PRIMARY KEY DEFAULT ksma_uuid(),
            tenant_id           uuid NOT NULL REFERENCES tenant(id) ON DELETE RESTRICT,
            unit_id             uuid NOT NULL REFERENCES education_unit(id) ON DELETE RESTRICT,
            batch_code          text NOT NULL,
            program_version_id  uuid NOT NULL REFERENCES program_version(id) ON DELETE RESTRICT,
            period_id           uuid REFERENCES academic_period(id),
            capacity            integer NOT NULL CHECK (capacity > 0),
            delivery_mode       text NOT NULL CHECK (delivery_mode IN ('onsite','online','hybrid')),
            service_from        date,
            service_until       date,
            status              text NOT NULL CHECK (status IN ('planned','open','running','closed','cancelled')),
            created_at          timestamptz NOT NULL DEFAULT now(),
            created_by          text NOT NULL,
            updated_at          timestamptz NOT NULL DEFAULT now(),
            updated_by          text NOT NULL,
            UNIQUE (unit_id, batch_code),
            CHECK (service_until IS NULL OR service_until >= service_from)
        )
        """
    )
    op.execute("CREATE INDEX ix_batch_tenant ON batch(tenant_id)")
    op.execute("CREATE INDEX ix_batch_unit ON batch(unit_id)")
    op.execute("CREATE INDEX ix_batch_program_version ON batch(program_version_id)")

    # application (U scope)
    op.execute(
        """
        CREATE TABLE application (
            id                  uuid PRIMARY KEY DEFAULT ksma_uuid(),
            tenant_id           uuid NOT NULL REFERENCES tenant(id) ON DELETE RESTRICT,
            unit_id             uuid NOT NULL REFERENCES education_unit(id) ON DELETE RESTRICT,
            participant_id      uuid NOT NULL REFERENCES participant(id) ON DELETE RESTRICT,
            program_id          uuid NOT NULL REFERENCES program(id) ON DELETE RESTRICT,
            requested_start     date,
            completeness        jsonb NOT NULL DEFAULT '[]'::jsonb,
            status              text NOT NULL CHECK (status IN
                                  ('draft','submitted','under_review','approved','rejected','withdrawn')),
            decision            jsonb,
            source_ref          text,
            created_at          timestamptz NOT NULL DEFAULT now(),
            created_by          text NOT NULL,
            updated_at          timestamptz NOT NULL DEFAULT now(),
            updated_by          text NOT NULL,
            CHECK (requested_start IS NULL OR requested_start >= CURRENT_DATE - INTERVAL '1 day')
        )
        """
    )
    op.execute("CREATE INDEX ix_application_tenant ON application(tenant_id)")
    op.execute(
        "CREATE INDEX ix_application_participant ON application(participant_id)"
    )
    op.execute("CREATE INDEX ix_application_program ON application(program_id)")

    # enrollment (U scope) — the showpiece table with the R7 exclusion constraint
    op.execute(
        """
        CREATE TABLE enrollment (
            id                  uuid PRIMARY KEY DEFAULT ksma_uuid(),
            tenant_id           uuid NOT NULL REFERENCES tenant(id) ON DELETE RESTRICT,
            unit_id             uuid NOT NULL REFERENCES education_unit(id) ON DELETE RESTRICT,
            participant_id      uuid NOT NULL REFERENCES participant(id) ON DELETE RESTRICT,
            batch_id            uuid NOT NULL REFERENCES batch(id) ON DELETE RESTRICT,
            application_id      uuid REFERENCES application(id),
            effective_range     tstzrange NOT NULL,
            status              text NOT NULL CHECK (status IN
                                  ('planned','active','paused','completed','withdrawn','transferred')),
            policy_id           uuid,
            created_at          timestamptz NOT NULL DEFAULT now(),
            created_by          text NOT NULL,
            updated_at          timestamptz NOT NULL DEFAULT now(),
            updated_by          text NOT NULL,
            CONSTRAINT uq_enrollment_no_overlap
              EXCLUDE USING gist (
                batch_id WITH =,
                participant_id WITH =,
                effective_range WITH &&
              ) WHERE (status IN ('planned','active'))
        )
        """
    )
    # The GiST operator class for uuid WITH = needs uuid_ops (provided by PG core).
    # For tstzrange WITH && we get it from built-in range support.
    op.execute("CREATE INDEX ix_enrollment_tenant ON enrollment(tenant_id)")
    op.execute("CREATE INDEX ix_enrollment_batch ON enrollment(batch_id)")
    op.execute("CREATE INDEX ix_enrollment_participant ON enrollment(participant_id)")

    # enrollment_change (U scope)
    op.execute(
        """
        CREATE TABLE enrollment_change (
            id                      uuid PRIMARY KEY DEFAULT ksma_uuid(),
            tenant_id               uuid NOT NULL REFERENCES tenant(id) ON DELETE RESTRICT,
            source_enrollment_id    uuid NOT NULL REFERENCES enrollment(id) ON DELETE RESTRICT,
            target_enrollment_id    uuid REFERENCES enrollment(id),
            change_kind             text NOT NULL CHECK (change_kind IN
                                      ('transfer','pause','resume','withdraw')),
            effective_at            timestamptz NOT NULL,
            decision                jsonb NOT NULL,
            status                  text NOT NULL CHECK (status IN
                                      ('proposed','approved','applied','rejected')),
            created_at              timestamptz NOT NULL DEFAULT now(),
            created_by              text NOT NULL,
            updated_at              timestamptz NOT NULL DEFAULT now(),
            updated_by              text NOT NULL
        )
        """
    )

    # teaching_assignment (U scope)
    op.execute(
        """
        CREATE TABLE teaching_assignment (
            id                  uuid PRIMARY KEY DEFAULT ksma_uuid(),
            tenant_id           uuid NOT NULL REFERENCES tenant(id) ON DELETE RESTRICT,
            unit_id             uuid NOT NULL REFERENCES education_unit(id) ON DELETE RESTRICT,
            membership_id       uuid NOT NULL REFERENCES membership(id) ON DELETE RESTRICT,
            batch_id            uuid REFERENCES batch(id),
            session_id          uuid,
            assignment_kind     text NOT NULL CHECK (assignment_kind IN
                                  ('instructor','substitute','assessor','approver')),
            component_codes     text[] NOT NULL DEFAULT '{}',
            valid_from          timestamptz NOT NULL,
            valid_until         timestamptz,
            status              text NOT NULL CHECK (status IN
                                  ('planned','active','ended','revoked')),
            created_at          timestamptz NOT NULL DEFAULT now(),
            created_by          text NOT NULL,
            updated_at          timestamptz NOT NULL DEFAULT now(),
            updated_by          text NOT NULL,
            CHECK (
              (batch_id IS NOT NULL AND session_id IS NULL) OR
              (batch_id IS NULL AND session_id IS NOT NULL)
            )
        )
        """
    )
    op.execute(
        "CREATE INDEX ix_teaching_assignment_membership "
        "ON teaching_assignment(membership_id)"
    )
    op.execute(
        "CREATE INDEX ix_teaching_assignment_batch "
        "ON teaching_assignment(batch_id) WHERE batch_id IS NOT NULL"
    )

    # RLS for every U table.
    rls_tables = [
        "batch",
        "application",
        "enrollment",
        "enrollment_change",
        "teaching_assignment",
    ]
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
    rls_tables = [
        "teaching_assignment",
        "enrollment_change",
        "enrollment",
        "application",
        "batch",
    ]
    for tbl in rls_tables:
        op.execute(f"DROP POLICY IF EXISTS {tbl}_tenant_isolation ON {tbl}")
        op.execute(f"ALTER TABLE {tbl} DISABLE ROW LEVEL SECURITY")
    op.execute("DROP TABLE IF EXISTS teaching_assignment CASCADE")
    op.execute("DROP TABLE IF EXISTS enrollment_change CASCADE")
    op.execute("DROP TABLE IF EXISTS enrollment CASCADE")
    op.execute("DROP TABLE IF EXISTS application CASCADE")
    op.execute("DROP TABLE IF EXISTS batch CASCADE")