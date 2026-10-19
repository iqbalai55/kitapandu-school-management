"""Finance context — BillingParty, Invoice, InvoiceLine, Installment, PaymentSubmission, VerifiedPayment, PaymentAllocation, Receipt, FinancialAdjustment

Revision ID: 0007
Revises: 0006
Create Date: 2026-10-20 02:30

Implements the money invariants (R12–R15, R21) at the DB layer:
- ``money`` columns are NUMERIC(18, 0) with CHECK (amount >= 0) on non-adjustment rows
- ``verified_payment.submission_id`` is UNIQUE → exactly one verified row per submission (R13)
- ``verified_payment.reversal_total`` is a projection column updated by
  FinancialAdjustment(kind='payment_reversal')
- ``payment_allocation`` is plain rows; ``reversed_at`` column marks reversals
  so available-funds sums remain trivial
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op

revision: str = "0007"
down_revision: Union[str, None] = "0006"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # billing_party (T scope)
    op.execute(
        """
        CREATE TABLE billing_party (
            id                  uuid PRIMARY KEY DEFAULT ksma_uuid(),
            tenant_id           uuid NOT NULL REFERENCES tenant(id) ON DELETE RESTRICT,
            party_type          text NOT NULL CHECK (party_type IN ('person','organization')),
            display_name        text NOT NULL,
            contact             jsonb,
            organization_ref    text,
            created_at          timestamptz NOT NULL DEFAULT now(),
            created_by          text NOT NULL,
            updated_at          timestamptz NOT NULL DEFAULT now(),
            updated_by          text NOT NULL
        )
        """
    )
    op.execute("CREATE INDEX ix_billing_party_tenant ON billing_party(tenant_id)")

    # invoice (U scope)
    op.execute(
        """
        CREATE TABLE invoice (
            id                  uuid PRIMARY KEY DEFAULT ksma_uuid(),
            tenant_id           uuid NOT NULL REFERENCES tenant(id) ON DELETE RESTRICT,
            unit_id             uuid NOT NULL REFERENCES education_unit(id) ON DELETE RESTRICT,
            participant_id      uuid NOT NULL REFERENCES participant(id) ON DELETE RESTRICT,
            payer_party_id      uuid REFERENCES billing_party(id),
            invoice_no          text,
            currency            text NOT NULL DEFAULT 'IDR' CHECK (currency = 'IDR'),
            document_status     text NOT NULL CHECK (document_status IN ('draft','issued','void')),
            issue_date          date,
            total_amount        numeric(18, 0) NOT NULL CHECK (total_amount >= 0),
            policy_id           uuid,
            created_at          timestamptz NOT NULL DEFAULT now(),
            created_by          text NOT NULL,
            updated_at          timestamptz NOT NULL DEFAULT now(),
            updated_by          text NOT NULL,
            UNIQUE (tenant_id, invoice_no)
        )
        """
    )
    op.execute("CREATE INDEX ix_invoice_tenant ON invoice(tenant_id)")
    op.execute("CREATE INDEX ix_invoice_participant ON invoice(participant_id)")

    # invoice_line (U scope)
    op.execute(
        """
        CREATE TABLE invoice_line (
            id                  uuid PRIMARY KEY DEFAULT ksma_uuid(),
            tenant_id           uuid NOT NULL REFERENCES tenant(id) ON DELETE RESTRICT,
            invoice_id          uuid NOT NULL REFERENCES invoice(id) ON DELETE RESTRICT,
            line_no             integer NOT NULL,
            description         text NOT NULL,
            enrollment_id       uuid REFERENCES enrollment(id),
            quantity            integer NOT NULL CHECK (quantity > 0),
            unit_price          numeric(18, 0) NOT NULL CHECK (unit_price >= 0),
            discount_amount     numeric(18, 0) NOT NULL DEFAULT 0 CHECK (discount_amount >= 0),
            net_amount          numeric(18, 0) NOT NULL CHECK (net_amount >= 0),
            created_at          timestamptz NOT NULL DEFAULT now(),
            created_by          text NOT NULL,
            UNIQUE (invoice_id, line_no)
        )
        """
    )

    # installment (U scope)
    op.execute(
        """
        CREATE TABLE installment (
            id                  uuid PRIMARY KEY DEFAULT ksma_uuid(),
            tenant_id           uuid NOT NULL REFERENCES tenant(id) ON DELETE RESTRICT,
            invoice_id          uuid NOT NULL REFERENCES invoice(id) ON DELETE RESTRICT,
            sequence_no         integer NOT NULL,
            due_date            date NOT NULL,
            due_amount          numeric(18, 0) NOT NULL CHECK (due_amount > 0),
            timezone            text NOT NULL DEFAULT 'Asia/Jakarta',
            created_at          timestamptz NOT NULL DEFAULT now(),
            created_by          text NOT NULL,
            UNIQUE (invoice_id, sequence_no)
        )
        """
    )

    # payment_submission (U scope)
    op.execute(
        """
        CREATE TABLE payment_submission (
            id                      uuid PRIMARY KEY DEFAULT ksma_uuid(),
            tenant_id               uuid NOT NULL REFERENCES tenant(id) ON DELETE RESTRICT,
            unit_id                 uuid NOT NULL REFERENCES education_unit(id) ON DELETE RESTRICT,
            participant_id          uuid NOT NULL REFERENCES participant(id) ON DELETE RESTRICT,
            invoice_id              uuid REFERENCES invoice(id),
            submitted_by            text NOT NULL,
            claimed_amount          numeric(18, 0) NOT NULL CHECK (claimed_amount > 0),
            currency                text NOT NULL DEFAULT 'IDR' CHECK (currency = 'IDR'),
            transfer_at             timestamptz,
            proof_attachment_id     uuid,
            transfer_reference      text,
            status                  text NOT NULL CHECK (status IN
                                        ('submitted','under_review','verified','rejected')),
            review_decision         jsonb,
            created_at              timestamptz NOT NULL DEFAULT now(),
            updated_at              timestamptz NOT NULL DEFAULT now()
        )
        """
    )
    op.execute(
        "CREATE INDEX ix_payment_submission_participant "
        "ON payment_submission(participant_id)"
    )

    # verified_payment (U scope)
    op.execute(
        """
        CREATE TABLE verified_payment (
            id                  uuid PRIMARY KEY DEFAULT ksma_uuid(),
            tenant_id           uuid NOT NULL REFERENCES tenant(id) ON DELETE RESTRICT,
            unit_id             uuid NOT NULL REFERENCES education_unit(id) ON DELETE RESTRICT,
            submission_id       uuid NOT NULL UNIQUE REFERENCES payment_submission(id),
            participant_id      uuid NOT NULL REFERENCES participant(id),
            recognized_amount    numeric(18, 0) NOT NULL CHECK (recognized_amount > 0),
            currency            text NOT NULL DEFAULT 'IDR' CHECK (currency = 'IDR'),
            verification        jsonb NOT NULL,
            received_date       date NOT NULL,
            reversal_total      numeric(18, 0) NOT NULL DEFAULT 0 CHECK (reversal_total >= 0),
            created_at          timestamptz NOT NULL DEFAULT now(),
            created_by          text NOT NULL
        )
        """
    )
    op.execute(
        "CREATE INDEX ix_verified_payment_participant "
        "ON verified_payment(participant_id)"
    )

    # payment_allocation (U scope)
    op.execute(
        """
        CREATE TABLE payment_allocation (
            id                  uuid PRIMARY KEY DEFAULT ksma_uuid(),
            tenant_id           uuid NOT NULL REFERENCES tenant(id) ON DELETE RESTRICT,
            payment_id          uuid NOT NULL REFERENCES verified_payment(id) ON DELETE RESTRICT,
            installment_id      uuid NOT NULL REFERENCES installment(id) ON DELETE RESTRICT,
            amount              numeric(18, 0) NOT NULL CHECK (amount > 0),
            allocated_at        timestamptz NOT NULL,
            allocated_by        text NOT NULL,
            idempotency_id      uuid,
            reversed_at         timestamptz,
            reversal_reason     text
        )
        """
    )
    op.execute(
        "CREATE INDEX ix_payment_allocation_payment "
        "ON payment_allocation(payment_id)"
    )
    op.execute(
        "CREATE INDEX ix_payment_allocation_installment "
        "ON payment_allocation(installment_id)"
    )

    # receipt (U scope)
    op.execute(
        """
        CREATE TABLE receipt (
            id                  uuid PRIMARY KEY DEFAULT ksma_uuid(),
            tenant_id           uuid NOT NULL REFERENCES tenant(id) ON DELETE RESTRICT,
            payment_id          uuid NOT NULL REFERENCES verified_payment(id) ON DELETE RESTRICT,
            receipt_no          text NOT NULL,
            snapshot            jsonb NOT NULL,
            status              text NOT NULL CHECK (status IN ('issued','replaced','void')),
            supersedes_id       uuid REFERENCES receipt(id),
            created_at          timestamptz NOT NULL DEFAULT now(),
            created_by          text NOT NULL,
            UNIQUE (tenant_id, receipt_no)
        )
        """
    )

    # financial_adjustment (U scope)
    op.execute(
        """
        CREATE TABLE financial_adjustment (
            id                  uuid PRIMARY KEY DEFAULT ksma_uuid(),
            tenant_id           uuid NOT NULL REFERENCES tenant(id) ON DELETE RESTRICT,
            adjustment_kind     text NOT NULL CHECK (adjustment_kind IN
                                  ('invoice_credit','allocation_reversal','payment_reversal')),
            source_kind         text NOT NULL,
            source_id           uuid NOT NULL,
            installment_id      uuid REFERENCES installment(id),
            amount              numeric(18, 0) NOT NULL CHECK (amount > 0),
            decision            jsonb NOT NULL,
            effective_at        timestamptz NOT NULL,
            idempotency_id      uuid,
            policy_id           uuid,
            created_at          timestamptz NOT NULL DEFAULT now(),
            created_by          text NOT NULL
        )
        """
    )
    op.execute(
        "CREATE INDEX ix_financial_adjustment_tenant_kind "
        "ON financial_adjustment(tenant_id, adjustment_kind)"
    )

    # RLS for every U table.
    rls_tables = [
        "billing_party",
        "invoice",
        "invoice_line",
        "installment",
        "payment_submission",
        "verified_payment",
        "payment_allocation",
        "receipt",
        "financial_adjustment",
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
        "financial_adjustment",
        "receipt",
        "payment_allocation",
        "verified_payment",
        "payment_submission",
        "installment",
        "invoice_line",
        "invoice",
        "billing_party",
    ]
    for tbl in rls_tables:
        op.execute(f"DROP POLICY IF EXISTS {tbl}_tenant_isolation ON {tbl}")
        op.execute(f"ALTER TABLE {tbl} DISABLE ROW LEVEL SECURITY")
    op.execute("DROP TABLE IF EXISTS financial_adjustment CASCADE")
    op.execute("DROP TABLE IF EXISTS receipt CASCADE")
    op.execute("DROP TABLE IF EXISTS payment_allocation CASCADE")
    op.execute("DROP TABLE IF EXISTS verified_payment CASCADE")
    op.execute("DROP TABLE IF EXISTS payment_submission CASCADE")
    op.execute("DROP TABLE IF EXISTS installment CASCADE")
    op.execute("DROP TABLE IF EXISTS invoice_line CASCADE")
    op.execute("DROP TABLE IF EXISTS invoice CASCADE")
    op.execute("DROP TABLE IF EXISTS billing_party CASCADE")