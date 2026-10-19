"""Portal + Operations contexts — 10 tables.

Revision ID: 0009
Revises: 0008
Create Date: 2026-10-20 02:40

Closes the pilot-acceptance scope: portal (PRT01–04) + operations (OPS01–04,
NFR07–10).

Tables:
- service_request
- internal_notification
- communication_preference
- attachment (NFR07)
- import_batch + import_row_result
- export_job
- metric_definition
- operational_run
- background_job
- processed_event (already in 0002 — skipped)
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op

revision: str = "0009"
down_revision: Union[str, None] = "0008"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # ---- Portal -------------------------------------------------------------
    op.execute(
        """
        CREATE TABLE service_request (
            id                      uuid PRIMARY KEY DEFAULT ksma_uuid(),
            tenant_id               uuid NOT NULL REFERENCES tenant(id) ON DELETE RESTRICT,
            requester_account_id    uuid NOT NULL REFERENCES account(id) ON DELETE RESTRICT,
            participant_id          uuid NOT NULL REFERENCES participant(id) ON DELETE RESTRICT,
            category                text NOT NULL CHECK (category IN ('academic','schedule','identity','finance')),
            source_ref              jsonb,
            description             text NOT NULL,
            assigned_membership_id  uuid REFERENCES membership(id),
            status                  text NOT NULL CHECK (status IN ('open','in_review','approved','rejected','closed')),
            resolution              jsonb,
            created_at              timestamptz NOT NULL DEFAULT now(),
            created_by              text NOT NULL,
            updated_at              timestamptz NOT NULL DEFAULT now(),
            updated_by              text NOT NULL
        )
        """
    )
    op.execute(
        "CREATE INDEX ix_service_request_tenant_status "
        "ON service_request(tenant_id, status)"
    )

    op.execute(
        """
        CREATE TABLE internal_notification (
            id                      uuid PRIMARY KEY DEFAULT ksma_uuid(),
            tenant_id               uuid NOT NULL REFERENCES tenant(id) ON DELETE RESTRICT,
            recipient_account_id    uuid NOT NULL REFERENCES account(id) ON DELETE RESTRICT,
            source_ref              jsonb NOT NULL,
            event_id                uuid,
            notification_kind       text NOT NULL,
            summary                 text NOT NULL,
            delivery_state          text NOT NULL CHECK (delivery_state IN ('pending','available','suppressed')),
            read_at                 timestamptz,
            occurred_at             timestamptz NOT NULL DEFAULT now(),
            UNIQUE (tenant_id, recipient_account_id, event_id, notification_kind)
        )
        """
    )
    op.execute(
        "CREATE INDEX ix_internal_notification_recipient "
        "ON internal_notification(recipient_account_id, occurred_at DESC)"
    )

    op.execute(
        """
        CREATE TABLE communication_preference (
            id                  uuid PRIMARY KEY DEFAULT ksma_uuid(),
            tenant_id           uuid NOT NULL REFERENCES tenant(id) ON DELETE RESTRICT,
            account_id          uuid NOT NULL REFERENCES account(id) ON DELETE RESTRICT,
            participant_id      uuid REFERENCES participant(id),
            category            text NOT NULL,
            channel             text NOT NULL CHECK (channel IN ('internal','manual_template')),
            enabled             boolean NOT NULL DEFAULT true,
            contact_reference   text,
            created_at          timestamptz NOT NULL DEFAULT now(),
            UNIQUE (tenant_id, account_id, participant_id, category, channel)
        )
        """
    )

    # ---- Operations ---------------------------------------------------------
    op.execute(
        """
        CREATE TABLE attachment (
            id                  uuid PRIMARY KEY DEFAULT ksma_uuid(),
            tenant_id           uuid NOT NULL REFERENCES tenant(id) ON DELETE RESTRICT,
            owner_ref           jsonb NOT NULL,
            storage_key         text NOT NULL,
            original_name       text NOT NULL,
            media_type          text NOT NULL,
            size_bytes          integer NOT NULL CHECK (size_bytes >= 0 AND size_bytes <= 10485760),
            content_hash        text,
            check_status        text NOT NULL CHECK (check_status IN
                                  ('pending','quarantined','approved','rejected','deleted')),
            retention_policy_id uuid REFERENCES policy_version(id),
            created_at          timestamptz NOT NULL DEFAULT now(),
            created_by          text NOT NULL
        )
        """
    )
    op.execute(
        "CREATE INDEX ix_attachment_tenant ON attachment(tenant_id)"
    )

    op.execute(
        """
        CREATE TABLE import_batch (
            id                  uuid PRIMARY KEY DEFAULT ksma_uuid(),
            tenant_id           uuid NOT NULL REFERENCES tenant(id) ON DELETE RESTRICT,
            requested_by        uuid NOT NULL REFERENCES account(id),
            dataset_kind        text NOT NULL,
            source_attachment_id uuid REFERENCES attachment(id),
            mapping             jsonb NOT NULL,
            scope               jsonb NOT NULL,
            status              text NOT NULL CHECK (status IN
                                  ('uploaded','validating','needs_correction','ready','committed','failed')),
            commit_request_id   uuid,
            counts              jsonb NOT NULL DEFAULT '{}'::jsonb,
            created_at          timestamptz NOT NULL DEFAULT now(),
            updated_at          timestamptz NOT NULL DEFAULT now()
        )
        """
    )

    op.execute(
        """
        CREATE TABLE import_row_result (
            id                  uuid PRIMARY KEY DEFAULT ksma_uuid(),
            tenant_id           uuid NOT NULL REFERENCES tenant(id) ON DELETE RESTRICT,
            import_batch_id     uuid NOT NULL REFERENCES import_batch(id) ON DELETE RESTRICT,
            row_no              integer NOT NULL,
            row_fingerprint     text NOT NULL,
            outcome             text NOT NULL CHECK (outcome IN
                                  ('valid','invalid','duplicate_candidate','committed')),
            errors              jsonb,
            target_ref          jsonb,
            source_reference    text,
            UNIQUE (import_batch_id, row_no)
        )
        """
    )

    op.execute(
        """
        CREATE TABLE export_job (
            id                  uuid PRIMARY KEY DEFAULT ksma_uuid(),
            tenant_id           uuid NOT NULL REFERENCES tenant(id) ON DELETE RESTRICT,
            requester_account_id uuid NOT NULL REFERENCES account(id),
            dataset_kind        text NOT NULL,
            scope               jsonb NOT NULL,
            filters             jsonb NOT NULL,
            as_of               timestamptz NOT NULL,
            status              text NOT NULL CHECK (status IN
                                  ('queued','running','ready','failed','cancelled','expired')),
            result_attachment_id uuid REFERENCES attachment(id),
            expires_at          timestamptz,
            created_at          timestamptz NOT NULL DEFAULT now(),
            updated_at          timestamptz NOT NULL DEFAULT now()
        )
        """
    )

    op.execute(
        """
        CREATE TABLE metric_definition (
            id                  uuid PRIMARY KEY DEFAULT ksma_uuid(),
            tenant_id           uuid NOT NULL REFERENCES tenant(id) ON DELETE RESTRICT,
            metric_code         text NOT NULL,
            version_no          integer NOT NULL CHECK (version_no > 0),
            label               text NOT NULL,
            source_spec         jsonb NOT NULL,
            time_basis          text NOT NULL CHECK (time_basis IN ('service_date','record_date','due_date')),
            effective_range     tstzrange NOT NULL,
            approval            jsonb NOT NULL,
            created_at          timestamptz NOT NULL DEFAULT now(),
            UNIQUE (tenant_id, metric_code, version_no)
        )
        """
    )

    op.execute(
        """
        CREATE TABLE operational_run (
            id                  uuid PRIMARY KEY DEFAULT ksma_uuid(),
            tenant_id           uuid REFERENCES tenant(id),
            operation_kind      text NOT NULL CHECK (operation_kind IN
                                  ('backup','restore_test','restore','retention','reconciliation')),
            environment         text NOT NULL CHECK (environment IN ('development','test','production')),
            execution_subject   uuid NOT NULL REFERENCES service_principal(id),
            authorization_ref   jsonb NOT NULL,
            started_at          timestamptz NOT NULL,
            finished_at         timestamptz,
            status              text NOT NULL CHECK (status IN ('running','succeeded','failed','cancelled')),
            evidence            jsonb,
            created_at          timestamptz NOT NULL DEFAULT now()
        )
        """
    )

    op.execute(
        """
        CREATE TABLE background_job (
            id                  uuid PRIMARY KEY DEFAULT ksma_uuid(),
            tenant_id           uuid REFERENCES tenant(id),
            event_id            uuid,
            job_kind            text NOT NULL,
            execution_subject   jsonb NOT NULL,
            scope               jsonb NOT NULL,
            status              text NOT NULL CHECK (status IN
                                  ('queued','running','succeeded','retry_wait','failed','cancelled')),
            attempt_count       integer NOT NULL DEFAULT 0 CHECK (attempt_count >= 0),
            next_attempt_at     timestamptz,
            result_ref          text,
            created_at          timestamptz NOT NULL DEFAULT now(),
            updated_at          timestamptz NOT NULL DEFAULT now()
        )
        """
    )

    # RLS for every T/U table.
    rls_tables = [
        "service_request",
        "internal_notification",
        "communication_preference",
        "attachment",
        "import_batch",
        "import_row_result",
        "export_job",
        "metric_definition",
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
        "background_job",
        "operational_run",
        "metric_definition",
        "export_job",
        "import_row_result",
        "import_batch",
        "attachment",
        "communication_preference",
        "internal_notification",
        "service_request",
    ]
    for tbl in rls_tables:
        op.execute(f"DROP POLICY IF EXISTS {tbl}_tenant_isolation ON {tbl}")
        op.execute(f"ALTER TABLE {tbl} DISABLE ROW LEVEL SECURITY")
    for tbl in rls_tables[::-1]:
        op.execute(f"DROP TABLE IF EXISTS {tbl} CASCADE")