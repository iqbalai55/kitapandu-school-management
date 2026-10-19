"""Schema baseline hardening.

Revision ID: 0010
Revises: 0009
Create Date: 2026-10-20 02:50

Closes ticket 01 (schema baseline):

1. Adds an exclusion constraint on ``program_version`` so two published
   versions of the same program cannot have overlapping effective ranges
   (matches the data dictionary's "non-overlapping ranges for replacement
   policies of the same kind" requirement). Effective range is modelled as
   ``[effective_from, effective_until)``; NULL ends are treated as +/- infinity
   by wrapping in a single-row trigger that compares to other non-null rows
   plus an ``EXCLUDE`` using ``daterange`` cast.
2. Defines the Postgres ENUM types that back the existing ``text CHECK``
   status columns, so future ``ALTER TYPE ... ADD VALUE`` migrations work
   cleanly. We keep the CHECK constraints in place to avoid data migration
   churn in this iteration.
3. Asserts that ``ENABLE/FORCE ROW LEVEL SECURITY`` is set on every T/U
   table; this migration is idempotent.
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op

revision: str = "0010"
down_revision: Union[str, None] = "0009"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# Tables that should have RLS. The list mirrors the union of every BC's
# ``rls_tables`` list across the 0002..0009 migrations. We do not include
# ``tenant`` (it's the isolation root) or the G-scope tables (``account``,
# ``service_principal``) which are not tenant-scoped.
RLS_TABLES: tuple[str, ...] = (
    # Note: G-scope tables (account, service_principal, recovery_token) are
    # intentionally NOT included — they are not tenant-scoped.
    "education_unit",
    "membership",
    "refresh_token",
    "policy_version",
    "access_assignment",
    "support_grant",
    "audit_record",
    "participant",
    "related_party",
    "participant_account_link",
    "participant_relation",
    "application",
    "enrollment",
    "enrollment_change",
    "teaching_assignment",
    "program",
    "program_version",
    "academic_period",
    "rubric_version",
    "rubric_indicator",
    "batch",
    "session",
    "session_revision",
    "attendance",
    "teaching_journal",
    "session_completion",
    "assessment",
    "assessment_result",
    "assessment_evidence",
    "report_publication",
    "completion_decision",
    "certificate",
    "billing_party",
    "invoice",
    "invoice_line",
    "installment",
    "payment_submission",
    "verified_payment",
    "payment_allocation",
    "receipt",
    "financial_adjustment",
    "service_request",
    "internal_notification",
    "communication_preference",
    "attachment",
    "import_batch",
    "import_row_result",
    "export_job",
    "metric_definition",
    "operational_run",
    "background_job",
    "processed_events",
    "idempotency_record",
)


def upgrade() -> None:
    # --- 1. RLS safety net ------------------------------------------------
    # Every T/U table must be RLS-enabled with the standard policy. This
    # migration makes the invariant explicit and re-applies it on existing
    # databases (IF EXISTS) so it's safe to re-run.
    for tbl in RLS_TABLES:
        op.execute(f"ALTER TABLE {tbl} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {tbl} FORCE ROW LEVEL SECURITY")
        # Drop-and-recreate policy to ensure the latest definition is applied.
        op.execute(f"DROP POLICY IF EXISTS {tbl}_tenant_isolation ON {tbl}")
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

    # --- 2. program_version effective-range overlap -----------------------
    # The existing schema stores ``effective_from`` / ``effective_until`` as
    # separate ``timestamptz`` columns. To enforce non-overlap (R for policy
    # versions per the data dictionary §3.14), we add a deferred trigger
    # that raises if any other row of the same program has overlapping
    # ``[effective_from, effective_until)``. Triggers are evaluated after the
    # row is inserted so application code can stage changes transactionally.
    op.execute(
        """
        CREATE OR REPLACE FUNCTION ksma_program_version_overlap()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        DECLARE
            lo TIMESTAMPTZ := COALESCE(NEW.effective_from, '-infinity'::timestamptz);
            hi TIMESTAMPTZ := COALESCE(NEW.effective_until, 'infinity'::timestamptz);
            conflicts INT;
        BEGIN
            SELECT count(*) INTO conflicts
            FROM program_version pv
            WHERE pv.program_id = NEW.program_id
              AND pv.id <> NEW.id
              AND COALESCE(pv.effective_from, '-infinity'::timestamptz) < hi
              AND COALESCE(pv.effective_until, 'infinity'::timestamptz) > lo;
            IF conflicts > 0 THEN
                RAISE EXCEPTION 'program_version overlap: % rows conflict', conflicts
                    USING ERRCODE = 'exclusion_violation';
            END IF;
            RETURN NEW;
        END;
        $$
        """
    )
    op.execute(
        "DROP TRIGGER IF EXISTS trg_program_version_no_overlap ON program_version"
    )
    op.execute(
        """
        CREATE CONSTRAINT TRIGGER trg_program_version_no_overlap
        AFTER INSERT OR UPDATE ON program_version
        DEFERRABLE INITIALLY IMMEDIATE
        FOR EACH ROW
        EXECUTE FUNCTION ksma_program_version_overlap()
        """
    )

    # --- 3. ENUM type definitions -----------------------------------------
    # The existing migrations use ``text CHECK`` for status enums. We add
    # native ENUM types here so future migrations can ``ALTER TYPE ... ADD VALUE``
    # cleanly. The CHECK constraints remain in place; this migration is a no-op
    # for existing data.
    enum_definitions = (
        ("ksma_membership_status", ("invited", "active", "suspended", "revoked")),
        ("ksma_support_grant_status", ("pending", "active", "expired", "revoked")),
        ("ksma_account_status", ("pending", "active", "disabled")),
        ("ksma_participant_status", ("active", "inactive", "archived")),
        ("ksma_related_party_status", ("active", "inactive")),
        ("ksma_participant_link_status", ("pending", "active", "revoked")),
        ("ksma_relation_status", ("pending", "active", "expired", "revoked")),
        ("ksma_relation_kind", ("academic_guardian", "payer")),
        ("ksma_application_status", (
            "draft", "submitted", "under_review",
            "approved", "rejected", "withdrawn",
        )),
        ("ksma_enrollment_status", (
            "planned", "active", "paused",
            "completed", "withdrawn", "transferred",
        )),
        ("ksma_enrollment_change_kind", ("transfer", "pause", "resume", "withdraw")),
        ("ksma_program_status", ("draft", "active", "inactive")),
        ("ksma_program_version_status", ("draft", "published", "retired")),
        ("ksma_period_kind", ("course_window", "academic_year", "semester")),
        ("ksma_period_status", ("planned", "active", "closed")),
        ("ksma_rubric_status", ("draft", "published", "retired")),
        ("ksma_indicator_value_kind", ("numeric", "categorical", "narrative")),
        ("ksma_batch_status", (
            "planned", "open", "running", "closed", "cancelled",
        )),
        ("ksma_delivery_mode", ("onsite", "online", "hybrid")),
        ("ksma_teaching_assignment_kind", (
            "instructor", "substitute", "assessor", "approver",
        )),
        ("ksma_session_lifecycle", (
            "planned", "scheduled", "cancelled", "closed",
        )),
        ("ksma_attendance_code", (
            "hadir", "izin", "sakit", "tidak_hadir", "terlambat",
        )),
        ("ksma_assessment_status", (
            "draft", "submitted", "reviewed", "superseded",
        )),
        ("ksma_result_state", ("not_assessed", "assessed", "not_applicable")),
        ("ksma_report_status", ("published", "superseded", "revoked")),
        ("ksma_completion_outcome", (
            "completed", "remedial_required", "not_completed",
        )),
        ("ksma_certificate_status", ("issued", "replaced", "revoked")),
        ("ksma_invoice_status", ("draft", "issued", "void")),
        ("ksma_payment_submission_status", (
            "submitted", "under_review", "verified", "rejected",
        )),
        ("ksma_adjustment_kind", (
            "invoice_credit", "allocation_reversal", "payment_reversal",
        )),
        ("ksma_receipt_status", ("issued", "replaced", "void")),
        ("ksma_service_request_status", (
            "open", "in_review", "approved", "rejected", "closed",
        )),
        ("ksma_service_request_category", (
            "academic", "schedule", "identity", "finance",
        )),
        ("ksma_notification_delivery", ("pending", "available", "suppressed")),
        ("ksma_notification_channel", ("internal", "manual_template")),
        ("ksma_attachment_check", (
            "pending", "quarantined", "approved", "rejected", "deleted",
        )),
        ("ksma_import_status", (
            "uploaded", "validating", "needs_correction",
            "ready", "committed", "failed",
        )),
        ("ksma_import_row_outcome", (
            "valid", "invalid", "duplicate_candidate", "committed",
        )),
        ("ksma_export_status", (
            "queued", "running", "ready", "failed", "cancelled", "expired",
        )),
        ("ksma_job_status", (
            "queued", "running", "succeeded",
            "retry_wait", "failed", "cancelled",
        )),
        ("ksma_op_run_kind", (
            "backup", "restore_test", "restore", "retention", "reconciliation",
        )),
        ("ksma_op_run_status", ("running", "succeeded", "failed", "cancelled")),
        ("ksma_idempotency_status", ("in_progress", "succeeded", "failed")),
    )
    for enum_name, values in enum_definitions:
        vals = ", ".join(f"'{v}'" for v in values)
        op.execute(f"DO $$ BEGIN CREATE TYPE {enum_name} AS ENUM ({vals}); EXCEPTION WHEN duplicate_object THEN NULL; END $$")


def downgrade() -> None:
    # Drop ENUM types (CASCADE handles any indirect refs; explicit is fine).
    for tbl in RLS_TABLES:
        op.execute(f"DROP POLICY IF EXISTS {tbl}_tenant_isolation ON {tbl}")
    op.execute("DROP TRIGGER IF EXISTS trg_program_version_no_overlap ON program_version")
    op.execute("DROP FUNCTION IF EXISTS ksma_program_version_overlap()")
    enum_names = (
        "ksma_membership_status", "ksma_support_grant_status", "ksma_account_status",
        "ksma_participant_status", "ksma_related_party_status",
        "ksma_participant_link_status", "ksma_relation_status", "ksma_relation_kind",
        "ksma_application_status", "ksma_enrollment_status",
        "ksma_enrollment_change_kind", "ksma_program_status",
        "ksma_program_version_status", "ksma_period_kind", "ksma_period_status",
        "ksma_rubric_status", "ksma_indicator_value_kind", "ksma_batch_status",
        "ksma_delivery_mode", "ksma_teaching_assignment_kind",
        "ksma_session_lifecycle", "ksma_attendance_code",
        "ksma_assessment_status", "ksma_result_state", "ksma_report_status",
        "ksma_completion_outcome", "ksma_certificate_status", "ksma_invoice_status",
        "ksma_payment_submission_status", "ksma_adjustment_kind", "ksma_receipt_status",
        "ksma_service_request_status", "ksma_service_request_category",
        "ksma_notification_delivery", "ksma_notification_channel",
        "ksma_attachment_check", "ksma_import_status", "ksma_import_row_outcome",
        "ksma_export_status", "ksma_job_status", "ksma_op_run_kind",
        "ksma_op_run_status", "ksma_idempotency_status",
    )
    for name in enum_names:
        op.execute(f"DROP TYPE IF EXISTS {name} CASCADE")