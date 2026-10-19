"""Assessment + Reporting contexts — 6 tables.

Revision ID: 0008
Revises: 0007
Create Date: 2026-10-20 02:35

Combined because the reporting context depends on assessment IDs (BR09, R11).
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op

revision: str = "0008"
down_revision: Union[str, None] = "0007"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # --- Assessment -----------------------------------------------------------
    op.execute(
        """
        CREATE TABLE assessment (
            id                  uuid PRIMARY KEY DEFAULT ksma_uuid(),
            tenant_id           uuid NOT NULL REFERENCES tenant(id) ON DELETE RESTRICT,
            unit_id             uuid NOT NULL REFERENCES education_unit(id) ON DELETE RESTRICT,
            enrollment_id       uuid NOT NULL REFERENCES enrollment(id) ON DELETE RESTRICT,
            rubric_version_id   uuid NOT NULL REFERENCES rubric_version(id) ON DELETE RESTRICT,
            assessor_id         uuid NOT NULL REFERENCES membership(id),
            assessment_date     date NOT NULL,
            status              text NOT NULL CHECK (status IN ('draft','submitted','reviewed','superseded')),
            supersedes_id       uuid REFERENCES assessment(id),
            created_at          timestamptz NOT NULL DEFAULT now(),
            created_by          text NOT NULL,
            updated_at          timestamptz NOT NULL DEFAULT now(),
            updated_by          text NOT NULL,
            CHECK (assessment_date <= CURRENT_DATE)
        )
        """
    )
    op.execute("CREATE INDEX ix_assessment_tenant ON assessment(tenant_id)")
    op.execute(
        "CREATE INDEX ix_assessment_enrollment ON assessment(enrollment_id)"
    )

    op.execute(
        """
        CREATE TABLE assessment_result (
            id                  uuid PRIMARY KEY DEFAULT ksma_uuid(),
            tenant_id           uuid NOT NULL REFERENCES tenant(id) ON DELETE RESTRICT,
            assessment_id       uuid NOT NULL REFERENCES assessment(id) ON DELETE RESTRICT,
            indicator_id        uuid NOT NULL REFERENCES rubric_indicator(id) ON DELETE RESTRICT,
            numeric_value       double precision,
            category_code       text,
            narrative_value     text,
            result_state        text NOT NULL CHECK (result_state IN
                                  ('not_assessed','assessed','not_applicable')),
            follow_up           text,
            created_at          timestamptz NOT NULL DEFAULT now(),
            created_by          text NOT NULL,
            updated_at          timestamptz NOT NULL DEFAULT now(),
            updated_by          text NOT NULL,
            UNIQUE (assessment_id, indicator_id),
            CHECK (
              ((numeric_value IS NOT NULL)::int + (category_code IS NOT NULL)::int + (narrative_value IS NOT NULL)::int) <= 1
            )
        )
        """
    )

    op.execute(
        """
        CREATE TABLE assessment_evidence (
            id                  uuid PRIMARY KEY DEFAULT ksma_uuid(),
            tenant_id           uuid NOT NULL REFERENCES tenant(id) ON DELETE RESTRICT,
            assessment_id       uuid NOT NULL REFERENCES assessment(id) ON DELETE RESTRICT,
            result_id           uuid REFERENCES assessment_result(id),
            attachment_id       uuid NOT NULL,
            caption             text,
            visibility          text NOT NULL DEFAULT 'internal_assessment'
                                  CHECK (visibility IN ('internal_assessment')),
            created_at          timestamptz NOT NULL DEFAULT now(),
            created_by          text NOT NULL
        )
        """
    )

    # --- Reporting ------------------------------------------------------------
    op.execute(
        """
        CREATE TABLE report_publication (
            id                      uuid PRIMARY KEY DEFAULT ksma_uuid(),
            tenant_id               uuid NOT NULL REFERENCES tenant(id) ON DELETE RESTRICT,
            unit_id                 uuid NOT NULL REFERENCES education_unit(id) ON DELETE RESTRICT,
            enrollment_id           uuid NOT NULL REFERENCES enrollment(id) ON DELETE RESTRICT,
            publication_no          integer NOT NULL CHECK (publication_no > 0),
            source_assessment_ids   uuid[] NOT NULL,
            snapshot                jsonb NOT NULL,
            publication             jsonb NOT NULL,
            attachment_id           uuid,
            supersedes_id           uuid REFERENCES report_publication(id),
            status                  text NOT NULL CHECK (status IN ('published','superseded','revoked')),
            created_at              timestamptz NOT NULL DEFAULT now(),
            created_by              text NOT NULL,
            updated_at              timestamptz NOT NULL DEFAULT now(),
            updated_by              text NOT NULL,
            UNIQUE (enrollment_id, publication_no)
        )
        """
    )
    op.execute(
        "CREATE INDEX ix_report_publication_enrollment "
        "ON report_publication(enrollment_id)"
    )

    op.execute(
        """
        CREATE TABLE completion_decision (
            id                  uuid PRIMARY KEY DEFAULT ksma_uuid(),
            tenant_id           uuid NOT NULL REFERENCES tenant(id) ON DELETE RESTRICT,
            unit_id             uuid NOT NULL REFERENCES education_unit(id) ON DELETE RESTRICT,
            enrollment_id       uuid NOT NULL REFERENCES enrollment(id) ON DELETE RESTRICT,
            outcome             text NOT NULL CHECK (outcome IN ('completed','remedial_required','not_completed')),
            evidence_refs       jsonb NOT NULL,
            policy_id           uuid REFERENCES policy_version(id),
            decision            jsonb NOT NULL,
            supersedes_id       uuid REFERENCES completion_decision(id),
            created_at          timestamptz NOT NULL DEFAULT now(),
            created_by          text NOT NULL,
            updated_at          timestamptz NOT NULL DEFAULT now(),
            updated_by          text NOT NULL
        )
        """
    )
    op.execute(
        "CREATE INDEX ix_completion_decision_enrollment "
        "ON completion_decision(enrollment_id)"
    )

    op.execute(
        """
        CREATE TABLE certificate (
            id                  uuid PRIMARY KEY DEFAULT ksma_uuid(),
            tenant_id           uuid NOT NULL REFERENCES tenant(id) ON DELETE RESTRICT,
            unit_id             uuid NOT NULL REFERENCES education_unit(id) ON DELETE RESTRICT,
            completion_id       uuid NOT NULL REFERENCES completion_decision(id),
            numbering_scope     text NOT NULL DEFAULT 'default',
            certificate_no      text NOT NULL,
            snapshot            jsonb NOT NULL,
            attachment_id       uuid,
            status              text NOT NULL CHECK (status IN ('issued','replaced','revoked')),
            replacement_id      uuid REFERENCES certificate(id),
            revocation          jsonb,
            created_at          timestamptz NOT NULL DEFAULT now(),
            created_by          text NOT NULL,
            UNIQUE (tenant_id, numbering_scope, certificate_no)
        )
        """
    )

    # RLS for every U table.
    rls_tables = [
        "assessment",
        "assessment_result",
        "assessment_evidence",
        "report_publication",
        "completion_decision",
        "certificate",
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
        "certificate",
        "completion_decision",
        "report_publication",
        "assessment_evidence",
        "assessment_result",
        "assessment",
    ]
    for tbl in rls_tables:
        op.execute(f"DROP POLICY IF EXISTS {tbl}_tenant_isolation ON {tbl}")
        op.execute(f"ALTER TABLE {tbl} DISABLE ROW LEVEL SECURITY")
    op.execute("DROP TABLE IF EXISTS certificate CASCADE")
    op.execute("DROP TABLE IF EXISTS completion_decision CASCADE")
    op.execute("DROP TABLE IF EXISTS report_publication CASCADE")
    op.execute("DROP TABLE IF EXISTS assessment_evidence CASCADE")
    op.execute("DROP TABLE IF EXISTS assessment_result CASCADE")
    op.execute("DROP TABLE IF EXISTS assessment CASCADE")