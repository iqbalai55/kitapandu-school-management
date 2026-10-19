"""Identity root — Tenant, EducationUnit, Account, Membership, ServicePrincipal, AccessAssignment, SupportGrant, PolicyVersion

Revision ID: 0002
Revises: 0001
Create Date: 2026-10-20 02:05

Creates the foundation tables from the KSMA Data Dictionary §3.1–§3.8 plus the
shared ``audit_record`` and ``idempotency_record`` tables. Applies tenant RLS
to every T/U table.
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
from sqlalchemy import text

revision: str = "0002"
down_revision: Union[str, None] = "0001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # -----------------------------------------------------------------------
    # tenant (ROOT scope)
    # -----------------------------------------------------------------------
    op.execute(
        """
        CREATE TABLE tenant (
            id                  uuid PRIMARY KEY DEFAULT ksma_uuid(),
            display_name        text NOT NULL,
            service_status      text NOT NULL CHECK (service_status IN ('setup','active','suspended')),
            default_timezone    text NOT NULL DEFAULT 'Asia/Jakarta',
            active_policy_id    uuid,
            activated_at        timestamptz,
            created_at          timestamptz NOT NULL DEFAULT now(),
            created_by          text NOT NULL,
            updated_at          timestamptz NOT NULL DEFAULT now(),
            updated_by          text NOT NULL
        )
        """
    )

    # -----------------------------------------------------------------------
    # education_unit (T scope)
    # -----------------------------------------------------------------------
    op.execute(
        """
        CREATE TABLE education_unit (
            id                  uuid PRIMARY KEY DEFAULT ksma_uuid(),
            tenant_id           uuid NOT NULL REFERENCES tenant(id) ON DELETE RESTRICT,
            unit_code           text NOT NULL,
            name                text NOT NULL,
            institution_type    text NOT NULL,
            education_levels    text[] NOT NULL DEFAULT '{}',
            location            jsonb,
            status              text NOT NULL CHECK (status IN ('active','inactive')),
            timezone            text NOT NULL DEFAULT 'Asia/Jakarta',
            created_at          timestamptz NOT NULL DEFAULT now(),
            created_by          text NOT NULL,
            updated_at          timestamptz NOT NULL DEFAULT now(),
            updated_by          text NOT NULL,
            UNIQUE (tenant_id, unit_code)
        )
        """
    )
    op.execute("CREATE INDEX ix_education_unit_tenant ON education_unit(tenant_id)")

    # -----------------------------------------------------------------------
    # account (G scope)
    # -----------------------------------------------------------------------
    op.execute(
        """
        CREATE TABLE account (
            id                  uuid PRIMARY KEY DEFAULT ksma_uuid(),
            auth_subject        text NOT NULL,
            auth_namespace      text NOT NULL,
            display_name        text NOT NULL,
            verified_contact    jsonb,
            password_hash       text,
            status              text NOT NULL CHECK (status IN ('pending','active','disabled')),
            authz_revision      integer NOT NULL DEFAULT 1,
            created_at          timestamptz NOT NULL DEFAULT now(),
            created_by          text NOT NULL,
            updated_at          timestamptz NOT NULL DEFAULT now(),
            updated_by          text NOT NULL,
            UNIQUE (auth_namespace, auth_subject)
        )
        """
    )

    # -----------------------------------------------------------------------
    # membership (T scope)
    # -----------------------------------------------------------------------
    op.execute(
        """
        CREATE TABLE membership (
            id                  uuid PRIMARY KEY DEFAULT ksma_uuid(),
            tenant_id           uuid NOT NULL REFERENCES tenant(id) ON DELETE RESTRICT,
            account_id          uuid NOT NULL REFERENCES account(id) ON DELETE RESTRICT,
            status              text NOT NULL CHECK (status IN ('invited','active','suspended','revoked')),
            valid_from          timestamptz NOT NULL DEFAULT now(),
            valid_until         timestamptz,
            invitation_ref      text,
            revocation          jsonb,
            created_at          timestamptz NOT NULL DEFAULT now(),
            created_by          text NOT NULL,
            updated_at          timestamptz NOT NULL DEFAULT now(),
            updated_by          text NOT NULL,
            UNIQUE (tenant_id, account_id)
        )
        """
    )
    op.execute("CREATE INDEX ix_membership_account ON membership(account_id)")
    op.execute("CREATE INDEX ix_membership_tenant ON membership(tenant_id)")

    # -----------------------------------------------------------------------
    # refresh_token (T scope, chained via replaced_by_id)
    # -----------------------------------------------------------------------
    op.execute(
        """
        CREATE TABLE refresh_token (
            id                  uuid PRIMARY KEY DEFAULT ksma_uuid(),
            tenant_id           uuid NOT NULL REFERENCES tenant(id) ON DELETE RESTRICT,
            account_id          uuid NOT NULL REFERENCES account(id) ON DELETE RESTRICT,
            membership_id       uuid NOT NULL REFERENCES membership(id) ON DELETE RESTRICT,
            token_hash          text NOT NULL UNIQUE,
            issued_at           timestamptz NOT NULL DEFAULT now(),
            expires_at          timestamptz NOT NULL,
            revoked_at          timestamptz,
            revoked_reason      text,
            replaced_by_id      uuid REFERENCES refresh_token(id),
            UNIQUE (id, replaced_by_id)
        )
        """
    )
    op.execute("CREATE INDEX ix_refresh_token_account ON refresh_token(account_id)")

    # -----------------------------------------------------------------------
    # recovery_token (T scope, single-use, time-bounded)
    # -----------------------------------------------------------------------
    op.execute(
        """
        CREATE TABLE recovery_token (
            id                  uuid PRIMARY KEY DEFAULT ksma_uuid(),
            account_id          uuid NOT NULL REFERENCES account(id) ON DELETE RESTRICT,
            token_hash          text NOT NULL UNIQUE,
            purpose             text NOT NULL CHECK (purpose IN ('password_recover','account_activate')),
            issued_at           timestamptz NOT NULL DEFAULT now(),
            expires_at          timestamptz NOT NULL,
            consumed_at         timestamptz,
            consumed_ip         inet
        )
        """
    )

    # -----------------------------------------------------------------------
    # service_principal (G scope)
    # -----------------------------------------------------------------------
    op.execute(
        """
        CREATE TABLE service_principal (
            id                  uuid PRIMARY KEY DEFAULT ksma_uuid(),
            principal_code      text NOT NULL UNIQUE,
            purpose             text NOT NULL,
            environment         text NOT NULL CHECK (environment IN ('development','test','production')),
            credential_ref      text NOT NULL,
            status              text NOT NULL CHECK (status IN ('active','disabled')),
            rotation_due_at     timestamptz,
            created_at          timestamptz NOT NULL DEFAULT now()
        )
        """
    )

    # -----------------------------------------------------------------------
    # policy_version (T scope)
    # -----------------------------------------------------------------------
    op.execute(
        """
        CREATE TABLE policy_version (
            id                  uuid PRIMARY KEY DEFAULT ksma_uuid(),
            tenant_id           uuid NOT NULL REFERENCES tenant(id) ON DELETE RESTRICT,
            policy_domain       text NOT NULL CHECK (policy_domain IN
                                    ('access','enrollment','session','assessment',
                                     'finance','communication','retention','certificate')),
            unit_scope_id       uuid REFERENCES education_unit(id),
            program_scope_id    uuid,
            version_no          integer NOT NULL CHECK (version_no > 0),
            effective_range     tstzrange NOT NULL,
            ruleset             jsonb NOT NULL,
            approval            jsonb,
            status              text NOT NULL CHECK (status IN ('draft','published','retired')),
            created_at          timestamptz NOT NULL DEFAULT now(),
            created_by          text NOT NULL,
            updated_at          timestamptz NOT NULL DEFAULT now(),
            updated_by          text NOT NULL,
            UNIQUE (tenant_id, policy_domain, version_no)
        )
        """
    )

    # Wire active_policy_id back-reference (deferred FK).
    op.execute(
        "ALTER TABLE tenant ADD CONSTRAINT tenant_active_policy_fk "
        "FOREIGN KEY (active_policy_id) REFERENCES policy_version(id)"
    )

    # -----------------------------------------------------------------------
    # access_assignment (T scope) — the GRANTS table that R02 evaluates.
    # -----------------------------------------------------------------------
    op.execute(
        """
        CREATE TABLE access_assignment (
            id                  uuid PRIMARY KEY DEFAULT ksma_uuid(),
            tenant_id           uuid NOT NULL REFERENCES tenant(id) ON DELETE RESTRICT,
            membership_id       uuid NOT NULL REFERENCES membership(id) ON DELETE RESTRICT,
            service_principal_id uuid REFERENCES service_principal(id),
            role_code           text NOT NULL,
            action_ids          text[] NOT NULL,
            scope               jsonb NOT NULL DEFAULT '{}'::jsonb,
            valid_from          timestamptz NOT NULL DEFAULT now(),
            valid_until         timestamptz,
            status              text NOT NULL CHECK (status IN ('pending','active','expired','revoked')),
            policy_id           uuid NOT NULL REFERENCES policy_version(id),
            "authorization"     jsonb NOT NULL,
            revocation          jsonb,
            created_at          timestamptz NOT NULL DEFAULT now(),
            created_by          text NOT NULL,
            updated_at          timestamptz NOT NULL DEFAULT now(),
            updated_by          text NOT NULL,
            CHECK (
              (membership_id IS NOT NULL AND service_principal_id IS NULL) OR
              (membership_id IS NULL AND service_principal_id IS NOT NULL)
            )
        )
        """
    )
    op.execute(
        "CREATE INDEX ix_access_assignment_member ON access_assignment(membership_id)"
    )
    op.execute(
        "CREATE INDEX ix_access_assignment_sp ON access_assignment(service_principal_id)"
    )
    op.execute("CREATE INDEX ix_access_assignment_tenant ON access_assignment(tenant_id)")

    # -----------------------------------------------------------------------
    # support_grant (T scope) — temporary platform-assistance grant
    # -----------------------------------------------------------------------
    op.execute(
        """
        CREATE TABLE support_grant (
            id                  uuid PRIMARY KEY DEFAULT ksma_uuid(),
            tenant_id           uuid NOT NULL REFERENCES tenant(id) ON DELETE RESTRICT,
            operator_account_id uuid NOT NULL REFERENCES account(id),
            ticket_ref          text NOT NULL,
            scope               jsonb NOT NULL,
            approved_by         jsonb NOT NULL,
            valid_from          timestamptz NOT NULL DEFAULT now(),
            valid_until         timestamptz NOT NULL,
            status              text NOT NULL CHECK (status IN ('pending','active','expired','revoked')),
            revocation          jsonb,
            created_at          timestamptz NOT NULL DEFAULT now()
        )
        """
    )

    # -----------------------------------------------------------------------
    # audit_record (T scope) — append-only
    # -----------------------------------------------------------------------
    op.execute(
        """
        CREATE TABLE audit_record (
            id                  uuid PRIMARY KEY DEFAULT ksma_uuid(),
            tenant_id           uuid NOT NULL,
            actor               jsonb NOT NULL,
            action_id           text NOT NULL,
            object_ref          jsonb NOT NULL,
            occurred_at         timestamptz NOT NULL DEFAULT now(),
            change_ref          jsonb,
            reason              text,
            authorization_ref   jsonb NOT NULL,
            correlation_id      text NOT NULL
        )
        """
    )
    op.execute("CREATE INDEX ix_audit_record_tenant_time ON audit_record(tenant_id, occurred_at)")
    # Append-only enforcement via revoke + rule:
    op.execute("REVOKE UPDATE, DELETE ON audit_record FROM PUBLIC")

    # -----------------------------------------------------------------------
    # idempotency_record (T scope)
    # -----------------------------------------------------------------------
    op.execute(
        """
        CREATE TABLE idempotency_record (
            id                  uuid PRIMARY KEY DEFAULT ksma_uuid(),
            tenant_id           uuid NOT NULL REFERENCES tenant(id) ON DELETE RESTRICT,
            request_subject     uuid NOT NULL,
            action_id           text NOT NULL,
            key_fingerprint     text NOT NULL,
            request_hash        text NOT NULL,
            status              text NOT NULL CHECK (status IN ('in_progress','succeeded','failed')),
            result_ref          text,
            expires_at          timestamptz NOT NULL,
            created_at          timestamptz NOT NULL DEFAULT now(),
            UNIQUE (tenant_id, request_subject, action_id, key_fingerprint)
        )
        """
    )

    # -----------------------------------------------------------------------
    # outbox_messages (T scope)
    # -----------------------------------------------------------------------
    op.execute(
        """
        CREATE TABLE outbox_messages (
            id                  uuid PRIMARY KEY DEFAULT ksma_uuid(),
            tenant_id           uuid NOT NULL REFERENCES tenant(id) ON DELETE RESTRICT,
            event_id            uuid NOT NULL,
            event_type          text NOT NULL,
            payload             jsonb NOT NULL,
            aggregate_ref       text,
            occurred_at         timestamptz NOT NULL DEFAULT now(),
            dispatch_state      text NOT NULL DEFAULT 'pending'
                                CHECK (dispatch_state IN ('pending','dispatched','failed')),
            dispatched_at       timestamptz,
            correlation_id      uuid
        )
        """
    )
    op.execute(
        "CREATE INDEX ix_outbox_pending ON outbox_messages(id) "
        "WHERE dispatch_state = 'pending'"
    )

    # -----------------------------------------------------------------------
    # processed_events (T scope) — consumer dedup per (tenant, consumer, event_id)
    # -----------------------------------------------------------------------
    op.execute(
        """
        CREATE TABLE processed_events (
            id                  uuid PRIMARY KEY DEFAULT ksma_uuid(),
            tenant_id           uuid NOT NULL REFERENCES tenant(id) ON DELETE RESTRICT,
            consumer_code       text NOT NULL,
            event_id            uuid NOT NULL,
            effect_ref          text,
            processed_at        timestamptz NOT NULL DEFAULT now(),
            UNIQUE (tenant_id, consumer_code, event_id)
        )
        """
    )

    # -----------------------------------------------------------------------
    # RLS POLICIES — every T/U table gets a policy that respects the GUCs.
    # -----------------------------------------------------------------------
    rls_tables = [
        "education_unit",
        "membership",
        "refresh_token",
        "policy_version",
        "access_assignment",
        "support_grant",
        "audit_record",
        "idempotency_record",
        "outbox_messages",
        "processed_events",
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
        "processed_events",
        "outbox_messages",
        "idempotency_record",
        "audit_record",
        "support_grant",
        "access_assignment",
        "policy_version",
        "refresh_token",
        "membership",
        "education_unit",
    ]
    for tbl in rls_tables:
        op.execute(f"DROP POLICY IF EXISTS {tbl}_tenant_isolation ON {tbl}")
        op.execute(f"ALTER TABLE {tbl} DISABLE ROW LEVEL SECURITY")
    op.execute("DROP TABLE IF EXISTS processed_events CASCADE")
    op.execute("DROP TABLE IF EXISTS outbox_messages CASCADE")
    op.execute("DROP TABLE IF EXISTS idempotency_record CASCADE")
    op.execute("DROP TABLE IF EXISTS audit_record CASCADE")
    op.execute("DROP TABLE IF EXISTS support_grant CASCADE")
    op.execute("DROP TABLE IF EXISTS access_assignment CASCADE")
    op.execute("ALTER TABLE tenant DROP CONSTRAINT IF EXISTS tenant_active_policy_fk")
    op.execute("DROP TABLE IF EXISTS policy_version CASCADE")
    op.execute("DROP TABLE IF EXISTS service_principal CASCADE")
    op.execute("DROP TABLE IF EXISTS recovery_token CASCADE")
    op.execute("DROP TABLE IF EXISTS refresh_token CASCADE")
    op.execute("DROP TABLE IF EXISTS membership CASCADE")
    op.execute("DROP TABLE IF EXISTS account CASCADE")
    op.execute("DROP TABLE IF EXISTS education_unit CASCADE")
    op.execute("DROP TABLE IF EXISTS tenant CASCADE")