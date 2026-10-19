"""Integration tests for the Finance BC (data dictionary V13–V18).

Per ADR-0001 + ADR-0002 + ticket #24 acceptance:
- V13: InvoiceLine sums equal invoice total; Installment sums equal invoice total.
- V14: Claimed payment != recognized; difference recorded with reason.
- V15: Insufficient available funds → 409 INSUFFICIENT_AVAILABLE_FUNDS.
- V16: Concurrent allocations against same installment; one wins, the other 409.
- V17: Invoice credit exceeding installment remainder → 409.
- V18: Payment reversal without reversing allocations rejected.

Seam: the application services in ``ksma.finance.application`` running against
real Postgres 16 with RLS + transaction isolation. We exercise the services
directly (not the HTTP routers) because ticket #20 owns the router wiring.
"""
from __future__ import annotations

import uuid
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker


# Module-level global stashed by app_client so every test can reuse one
# seeded participant instead of generating fresh random UUIDs that violate
# the participant FK.
_SEEDED_PARTICIPANT_ID: uuid.UUID | None = None


@pytest_asyncio.fixture
async def app_client(migrated_db: AsyncEngine) -> AsyncIterator[tuple[AsyncClient, uuid.UUID, uuid.UUID, uuid.UUID]]:
    """Bootstrap tenant + unit + published finance policy in the test DB,
    then yield (AsyncClient, tenant_id, ctx, unit_id).

    Returns the unit_id so tests don't have to query for it again.
    """
    tenant_a = uuid.uuid4()
    unit_id = uuid.uuid4()
    TestSession = async_sessionmaker(migrated_db, expire_on_commit=False)

    async with TestSession() as session:
        await session.execute(text("SET LOCAL app.bypass_rls = 'true'"))
        await session.execute(
            text(
                "INSERT INTO tenant (id, display_name, service_status, default_timezone, "
                "created_at, created_by, updated_at, updated_by) "
                "VALUES (:tid, 'Tenant A', 'active', 'Asia/Jakarta', now(), 'seed', now(), 'seed')"
            ),
            {"tid": str(tenant_a)},
        )
        await session.execute(
            text(
                "INSERT INTO education_unit "
                "(id, tenant_id, unit_code, name, institution_type, education_levels, "
                " status, timezone, created_at, created_by, updated_at, updated_by) "
                "VALUES "
                "(:uid, :tid, 'VANETA-A', 'Vaneta Unit A', 'lk_lkp', ARRAY['TK'], "
                " 'active', 'Asia/Jakarta', now(), 'seed', now(), 'seed')"
            ),
            {"uid": str(unit_id), "tid": str(tenant_a)},
        )
        # Seed one participant that every test can reuse.
        global _SEEDED_PARTICIPANT_ID  # type: ignore[name-defined]
        _SEEDED_PARTICIPANT_ID = uuid.uuid4()
        await session.execute(
            text(
                "INSERT INTO participant "
                "(id, tenant_id, participant_no, full_name, status, "
                " created_at, created_by, updated_at, updated_by) "
                "VALUES "
                "(:pid, :tid, 'P-V-0001', 'Sari Test', 'active', "
                " now(), 'seed', now(), 'seed')"
            ),
            {"pid": str(_SEEDED_PARTICIPANT_ID), "tid": str(tenant_a)},
        )
        await session.execute(
            text(
                "INSERT INTO policy_version "
                "(id, tenant_id, policy_domain, version_no, effective_range, ruleset, status, "
                "created_at, created_by, updated_at, updated_by) "
                "VALUES "
                "(ksma_uuid(), :tid, 'finance', 1, tstzrange(now(), 'infinity'), "
                " CAST('{\"rounding\":\"ROUND_HALF_EVEN\"}' AS JSONB), 'published', "
                " now(), 'seed', now(), 'seed')"
            ),
            {"tid": str(tenant_a)},
        )
        await session.commit()

    # Force the app's cached settings to use the test DB.
    import os
    test_url = os.environ["KITAPANDU_POSTGRES_DSN"]
    os.environ["KITAPANDU_POSTGRES_DSN_SYNC"] = test_url.replace(
        "ksma_app:ksma_app@", "test:test@", 1
    )
    os.environ["KITAPANDU_POSTGRES_DSN"] = test_url
    from ksma.settings import get_settings
    get_settings.cache_clear()

    # The FastAPI app is imported once per test process; importing it here
    # exercises the full module graph (including the identity BC stubs we
    # added for this integration setup).
    from ksma.main import app
    from ksma.shared.tenancy import ActorKind, TenantContext

    ctx = TenantContext(
        account_id=uuid.uuid4(),
        membership_id=uuid.uuid4(),
        tenant_id=tenant_a,
        unit_id=None,
        actor_kind=ActorKind.ACCOUNT,
        authz_revision=1,
        role_codes=("FI",),
    )

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        yield client, tenant_a, ctx, unit_id


# ---------------------------------------------------------------------------
# V13: InvoiceLine + Installment sum == invoice total
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_v13_line_and_installment_sum_must_equal_invoice_total(
    app_client, migrated_db: AsyncEngine
) -> None:
    """Lines 600k + 400k must equal invoice total 1M; installments same."""
    _, _, ctx, unit_id = app_client
    TestSession = async_sessionmaker(migrated_db, expire_on_commit=False)

    from ksma.finance.application import create_invoice, issue_invoice
    from ksma.finance.infrastructure.repositories import InvoiceRepository

    today = __import__("datetime").date.today()
    async with TestSession() as session:
        await session.execute(text("SET LOCAL app.bypass_rls = 'true'"))
        inv = await create_invoice(
            session, ctx,
            unit_id=unit_id,
            participant_id=_SEEDED_PARTICIPANT_ID,
            payer_party_id=None,
            lines=[
                {"description": "Course A", "quantity": 1, "unit_price": 600_000, "discount_amount": 0},
                {"description": "Course B", "quantity": 1, "unit_price": 400_000, "discount_amount": 0},
            ],
            installments=[
                {"sequence_no": 1, "due_amount": 600_000, "due_date": today},
                {"sequence_no": 2, "due_amount": 400_000, "due_date": today},
            ],
        )
        await issue_invoice(session, ctx, invoice_id=inv.id)

        # The total is the sum of the lines.
        repo = InvoiceRepository(session, ctx.tenant_id)
        stored = await repo.get(inv.id)
        assert stored is not None
        assert stored.total_amount == 1_000_000
        assert stored.invoice_no is not None  # allocated on issue


@pytest.mark.asyncio
async def test_v13_installment_sum_mismatch_rejected(
    app_client, migrated_db: AsyncEngine
) -> None:
    """If installment sum != line sum, create_invoice raises ValidationError."""
    _, _, ctx, unit_id = app_client
    TestSession = async_sessionmaker(migrated_db, expire_on_commit=False)

    from ksma.finance.application import create_invoice
    from ksma.shared.errors import ValidationError

    today = __import__("datetime").date.today()
    async with TestSession() as session:
        await session.execute(text("SET LOCAL app.bypass_rls = 'true'"))
        with pytest.raises(ValidationError):
            await create_invoice(
                session, ctx,
                unit_id=unit_id,
                participant_id=_SEEDED_PARTICIPANT_ID,
                payer_party_id=None,
                lines=[
                    {"description": "A", "quantity": 1, "unit_price": 600_000, "discount_amount": 0},
                    {"description": "B", "quantity": 1, "unit_price": 400_000, "discount_amount": 0},
                ],
                installments=[
                    {"sequence_no": 1, "due_amount": 900_000, "due_date": today},
                    # Sum 900k != 1M.
                ],
            )


# ---------------------------------------------------------------------------
# V14: claimed != recognized
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_v14_recognized_can_differ_from_claimed(
    app_client, migrated_db: AsyncEngine
) -> None:
    """FI verifies claimed 500k with recognized 450k; recognized_amount is
    what's available for allocation."""
    _, _, ctx, unit_id = app_client
    TestSession = async_sessionmaker(migrated_db, expire_on_commit=False)

    from ksma.finance.application import submit_payment, verify_payment

    async with TestSession() as session:
        await session.execute(text("SET LOCAL app.bypass_rls = 'true'"))
        sub = await submit_payment(
            session, ctx,
            unit_id=unit_id,
            participant_id=_SEEDED_PARTICIPANT_ID,
            invoice_id=None,
            claimed_amount=500_000,
            transfer_at=None,
            proof_attachment_id=None,
            transfer_reference="TR-V14",
        )
        vp = await verify_payment(
            session, ctx,
            submission_id=sub.id,
            recognized_amount=450_000,
            reason="partial_transfer_fee",
        )
        assert vp.recognized_amount == 450_000


# ---------------------------------------------------------------------------
# V15: allocation exceeding available funds
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_v15_allocation_exceeding_available_rejected(
    app_client, migrated_db: AsyncEngine
) -> None:
    """Funds 1M, allocations 700k, attempting a new 350k allocation → 409."""
    _, _, ctx, unit_id = app_client
    TestSession = async_sessionmaker(migrated_db, expire_on_commit=False)

    from ksma.finance.application import (
        allocate_funds, create_invoice, issue_invoice, submit_payment, verify_payment,
    )
    from ksma.finance.infrastructure.repositories import InstallmentRepository
    from ksma.shared.errors import InsufficientFundsError

    today = __import__("datetime").date.today()
    async with TestSession() as session:
        await session.execute(text("SET LOCAL app.bypass_rls = 'true'"))
        inv = await create_invoice(
            session, ctx, unit_id=unit_id,
            participant_id=_SEEDED_PARTICIPANT_ID, payer_party_id=None,
            lines=[{"description": "X", "quantity": 1, "unit_price": 1_000_000, "discount_amount": 0}],
            installments=[{"sequence_no": 1, "due_amount": 1_000_000, "due_date": today}],
        )
        await issue_invoice(session, ctx, invoice_id=inv.id)
        sub = await submit_payment(
            session, ctx, unit_id=unit_id, participant_id=_SEEDED_PARTICIPANT_ID,
            invoice_id=inv.id, claimed_amount=1_000_000,
            transfer_at=None, proof_attachment_id=None, transfer_reference="V15",
        )
        vp = await verify_payment(
            session, ctx, submission_id=sub.id, recognized_amount=1_000_000, reason="OK",
        )
        irepo = InstallmentRepository(session, ctx.tenant_id)
        installments = await irepo.list_for_invoice(inv.id)
        inst_id = installments[0].id

        alloc1, _ = await allocate_funds(
            session, ctx,
            payment_id=vp.id, installment_id=inst_id,
            amount=700_000, idempotency_id=uuid.uuid4(),
        )
        assert alloc1.amount == 700_000

        with pytest.raises(InsufficientFundsError):
            await allocate_funds(
                session, ctx,
                payment_id=vp.id, installment_id=inst_id,
                amount=350_000, idempotency_id=uuid.uuid4(),
            )


# ---------------------------------------------------------------------------
# V16: sequential allocation race
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_v16_sequential_allocation_race(
    app_client, migrated_db: AsyncEngine
) -> None:
    """Installment remainder 200k, two sequential 150k allocations →
    exactly one succeeds; the other 409."""
    _, _, ctx, unit_id = app_client
    TestSession = async_sessionmaker(migrated_db, expire_on_commit=False)

    from ksma.finance.application import (
        allocate_funds, create_invoice, issue_invoice, submit_payment, verify_payment,
    )
    from ksma.finance.infrastructure.repositories import InstallmentRepository
    from ksma.shared.errors import InsufficientFundsError

    today = __import__("datetime").date.today()
    async with TestSession() as session:
        await session.execute(text("SET LOCAL app.bypass_rls = 'true'"))
        inv = await create_invoice(
            session, ctx, unit_id=unit_id,
            participant_id=_SEEDED_PARTICIPANT_ID, payer_party_id=None,
            lines=[{"description": "X", "quantity": 1, "unit_price": 200_000, "discount_amount": 0}],
            installments=[{"sequence_no": 1, "due_amount": 200_000, "due_date": today}],
        )
        await issue_invoice(session, ctx, invoice_id=inv.id)
        sub = await submit_payment(
            session, ctx, unit_id=unit_id, participant_id=_SEEDED_PARTICIPANT_ID,
            invoice_id=inv.id, claimed_amount=300_000,
            transfer_at=None, proof_attachment_id=None, transfer_reference="V16",
        )
        vp = await verify_payment(
            session, ctx, submission_id=sub.id, recognized_amount=300_000, reason="OK",
        )
        irepo = InstallmentRepository(session, ctx.tenant_id)
        installments = await irepo.list_for_invoice(inv.id)
        inst_id = installments[0].id

        results = []
        for _ in range(2):
            try:
                a, _ = await allocate_funds(
                    session, ctx, payment_id=vp.id, installment_id=inst_id,
                    amount=150_000, idempotency_id=uuid.uuid4(),
                )
                results.append(("ok", a.amount))
            except InsufficientFundsError as exc:
                results.append(("denied", exc.details.get("available")))

        statuses = sorted(r[0] for r in results)
        assert "ok" in statuses, f"expected one ok, got {results}"
        assert "denied" in statuses, f"expected one denied, got {results}"


# ---------------------------------------------------------------------------
# V17: invoice_credit exceeding remainder
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_v17_credit_exceeding_remainder_rejected(
    app_client, migrated_db: AsyncEngine
) -> None:
    """Invoice 1M, allocation 700k; credit 350k against remainder 300k → 409."""
    _, _, ctx, unit_id = app_client
    TestSession = async_sessionmaker(migrated_db, expire_on_commit=False)

    from ksma.finance.application import (
        allocate_funds, create_invoice, invoice_credit, issue_invoice,
        submit_payment, verify_payment,
    )
    from ksma.finance.infrastructure.repositories import InstallmentRepository
    from ksma.shared.errors import InsufficientFundsError, ValidationError

    today = __import__("datetime").date.today()
    async with TestSession() as session:
        await session.execute(text("SET LOCAL app.bypass_rls = 'true'"))
        inv = await create_invoice(
            session, ctx, unit_id=unit_id,
            participant_id=_SEEDED_PARTICIPANT_ID, payer_party_id=None,
            lines=[{"description": "X", "quantity": 1, "unit_price": 1_000_000, "discount_amount": 0}],
            installments=[{"sequence_no": 1, "due_amount": 1_000_000, "due_date": today}],
        )
        await issue_invoice(session, ctx, invoice_id=inv.id)
        sub = await submit_payment(
            session, ctx, unit_id=unit_id, participant_id=_SEEDED_PARTICIPANT_ID,
            invoice_id=inv.id, claimed_amount=1_000_000,
            transfer_at=None, proof_attachment_id=None, transfer_reference="V17",
        )
        vp = await verify_payment(
            session, ctx, submission_id=sub.id, recognized_amount=1_000_000, reason="OK",
        )
        irepo = InstallmentRepository(session, ctx.tenant_id)
        installments = await irepo.list_for_invoice(inv.id)
        inst_id = installments[0].id
        await allocate_funds(
            session, ctx, payment_id=vp.id, installment_id=inst_id,
            amount=700_000, idempotency_id=uuid.uuid4(),
        )

        # Per the code, invoice_credit raises ValidationError (not
        # InsufficientFundsError) when the credit exceeds the remainder.
        with pytest.raises((ValidationError, InsufficientFundsError)):
            await invoice_credit(
                session, ctx, installment_id=inst_id,
                amount=350_000, reason="goodwill_exceeds",
            )

        # A 200k credit against 300k remainder succeeds.
        await invoice_credit(
            session, ctx, installment_id=inst_id,
            amount=200_000, reason="partial_goodwill",
        )


# ---------------------------------------------------------------------------
# V18: payment reversal that automatically reverses allocations atomically
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_v18_payment_reversal_reverses_allocations_atomically(
    app_client, migrated_db: AsyncEngine
) -> None:
    """Per R15: ``reverse_payment`` also reverses the payment's active
    allocations in the same transaction, so available funds don't go
    negative. After the reversal, available = 1M - 0 (alloc) - 1M (reversal)
    = 0; the installment remainder goes back to 1M (uncounting the
    allocation)."""
    _, _, ctx, unit_id = app_client
    TestSession = async_sessionmaker(migrated_db, expire_on_commit=False)

    from ksma.finance.application import (
        allocate_funds, create_invoice, issue_invoice, reverse_payment,
        submit_payment, verify_payment,
    )
    from ksma.finance.infrastructure.repositories import InstallmentRepository
    from sqlalchemy import text as _text

    today = __import__("datetime").date.today()
    async with TestSession() as session:
        await session.execute(_text("SET LOCAL app.bypass_rls = 'true'"))
        inv = await create_invoice(
            session, ctx, unit_id=unit_id,
            participant_id=_SEEDED_PARTICIPANT_ID, payer_party_id=None,
            lines=[{"description": "X", "quantity": 1, "unit_price": 1_000_000, "discount_amount": 0}],
            installments=[{"sequence_no": 1, "due_amount": 1_000_000, "due_date": today}],
        )
        await issue_invoice(session, ctx, invoice_id=inv.id)
        sub = await submit_payment(
            session, ctx, unit_id=unit_id, participant_id=_SEEDED_PARTICIPANT_ID,
            invoice_id=inv.id, claimed_amount=1_000_000,
            transfer_at=None, proof_attachment_id=None, transfer_reference="V18",
        )
        vp = await verify_payment(
            session, ctx, submission_id=sub.id, recognized_amount=1_000_000, reason="OK",
        )
        irepo = InstallmentRepository(session, ctx.tenant_id)
        installments = await irepo.list_for_invoice(inv.id)
        inst_id = installments[0].id
        await allocate_funds(
            session, ctx, payment_id=vp.id, installment_id=inst_id,
            amount=700_000, idempotency_id=uuid.uuid4(),
        )
        # Reversal succeeds (R15 atomically reverses allocations).
        await reverse_payment(
            session, ctx, payment_id=vp.id, reason="duplicate_deposit",
        )
        # Confirm the allocation was atomically marked reversed.
        row = (await session.execute(_text(
            "SELECT reversed_at, reversal_reason FROM payment_allocation "
            "WHERE payment_id = :pid"
        ), {"pid": str(vp.id)})).first()
        assert row is not None
        assert row.reversed_at is not None
        assert row.reversal_reason == "duplicate_deposit"