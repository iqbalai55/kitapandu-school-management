"""Unit tests for ``ksma.finance.application`` — invoice + allocation seam.

Pins ONE behavior per test class (issue #43, P5.2.7):

- ``TestCreateInvoice``: line math (V13 — 600k+400k=1M), quantity/price/
  discount/net validation guards, R12 installment-sum equality.
- ``TestVoidInvoice``: V18 — void rejected when active allocations exist.
- ``TestAllocateFunds``: V15 capacity exceeded; V16 same-key idempotency.

Persistence is the in-memory ``FakeAsyncSession`` from
``tests/unit/conftest_helpers.py``. The policy-version SELECT is monkey-
patched at the module level so we never need to seed a ``policy_version``
row. Repository helpers whose SQL joins aliased columns
(``InvoiceRepository.sum_active_allocations``) are also patched at the
class level — the fake's WHERE filter does not understand table aliases,
so a row of ``payment_allocation`` keyed by ``i.invoice_id`` is harder
to compose than a one-line class patch.
"""
from __future__ import annotations

from datetime import date
from typing import Any
from uuid import UUID, uuid4

import pytest

from tests.unit.conftest_helpers import FakeAsyncSession, FakeTenantContext

from ksma.finance import application as fin_app
from ksma.finance.application import (
    allocate_funds,
    create_invoice,
    void_invoice,
)
from ksma.finance.domain import InvoiceDocumentStatus
from ksma.finance.infrastructure.repositories import (
    InvoiceRepository,
    VerifiedPaymentRepository,
)
from ksma.shared.errors import (
    InsufficientFundsError,
    StateConflictError,
    ValidationError,
)
from ksma.shared.rbac import RoleCode


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _build_ctx(
    tenant_id: UUID | None = None,
    account_id: UUID | None = None,
    membership_id: UUID | None = None,
) -> Any:
    """Build an ACCOUNT ``TenantContext`` with explicit ids."""
    builder = FakeTenantContext(
        RoleCode.AC, tenant_id=tenant_id
    ).with_account(account_id or uuid4(), membership_id or uuid4())
    return builder.build()


@pytest.fixture(autouse=True)
def _stub_policy(monkeypatch: pytest.MonkeyPatch) -> UUID:
    """Bypass the ``policy_version`` SELECT for every test in this module.

    ``create_invoice`` is the only function that calls
    ``_require_published_finance_policy``; we patch the module-level helper
    so the test doesn't need to seed a ``policy_version`` row.
    """
    policy_id = uuid4()

    async def _fake_policy(session: Any, tid: UUID) -> UUID:
        return policy_id

    monkeypatch.setattr(fin_app, "_require_published_finance_policy", _fake_policy)
    return policy_id


def _seed_invoice(
    session: FakeAsyncSession,
    tenant_id: UUID,
    *,
    invoice_id: UUID | None = None,
    status: InvoiceDocumentStatus = InvoiceDocumentStatus.ISSUED,
    total: int = 1_000_000,
) -> UUID:
    """Seed one row in the ``invoice`` table for void_invoice tests.

    The row carries every column ``_to_invoice`` reads (so
    ``lock_for_update`` returns a fully-populated Invoice). UPDATE keeps
    the column-name keys (``document_status``), not the bind-param keys.
    """
    invoice_id = invoice_id or uuid4()
    session.seed(
        "invoice",
        {
            "id": invoice_id,
            "tenant_id": tenant_id,
            "unit_id": uuid4(),
            "participant_id": uuid4(),
            "payer_party_id": None,
            "invoice_no": "INV-000001",
            "currency": "IDR",
            "document_status": status.value,
            "issue_date": date(2025, 1, 1),
            "total_amount": total,
            "policy_id": uuid4(),
            "created_at": None,
            "created_by": "test",
            "updated_at": None,
            "updated_by": "test",
        },
    )
    return invoice_id


def _seed_vp_inst(
    session: FakeAsyncSession,
    tenant_id: UUID,
    *,
    vp_id: UUID | None = None,
    inst_id: UUID | None = None,
    recognized: int = 1_000_000,
    due_amount: int = 1_000_000,
) -> tuple[UUID, UUID]:
    """Seed a ``verified_payment`` + an ``installment`` for allocate_funds tests."""
    vp_id = vp_id or uuid4()
    inst_id = inst_id or uuid4()
    session.seed(
        "verified_payment",
        {
            "id": vp_id,
            "tenant_id": tenant_id,
            "unit_id": uuid4(),
            "submission_id": uuid4(),
            "participant_id": uuid4(),
            "recognized_amount": recognized,
            "currency": "IDR",
            "verification": {"actor": str(uuid4())},
            "received_date": date(2025, 1, 1),
            "reversal_total": 0,
            "created_at": None,
            "created_by": "test",
        },
    )
    session.seed(
        "installment",
        {
            "id": inst_id,
            "tenant_id": tenant_id,
            "invoice_id": uuid4(),
            "sequence_no": 1,
            "due_date": date(2025, 1, 1),
            "due_amount": due_amount,
            "timezone": "Asia/Jakarta",
            "created_at": None,
            "created_by": "test",
        },
    )
    return vp_id, inst_id


# ===========================================================================
# create_invoice
# ===========================================================================


class TestCreateInvoice:
    """``create_invoice`` — line math + per-line validation + R12 sum."""

    async def test_line_math_600k_plus_400k_equals_1m(self) -> None:
        """V13: total = sum(qty * unit_price - discount) across lines.

        Two lines totalling 600k + 400k = 1M pin both the per-line net
        computation on line 143 (``qty * price - disc``) and the
        equivalent INSERT-side recomputation on line 172. Mutants
        ``* -> /``, ``- -> +``, ``0 -> 1`` on either line flip it.
        """
        session = FakeAsyncSession()
        tenant_id = uuid4()
        ctx = _build_ctx(tenant_id=tenant_id)
        inv = await create_invoice(
            session,
            ctx,
            unit_id=uuid4(),
            participant_id=uuid4(),
            lines=[
                {
                    "description": "tuition",
                    "quantity": 1,
                    "unit_price": 600_000,
                    "discount_amount": 0,
                },
                {
                    "description": "books",
                    "quantity": 1,
                    "unit_price": 400_000,
                    "discount_amount": 0,
                },
            ],
        )
        # The computed total is the headline invariant.
        assert inv.total_amount == 1_000_000
        # The invoice row carries the computed total via bind-param ``:total``.
        inv_rows = session.rows("invoice")
        assert len(inv_rows) == 1
        assert int(inv_rows[0]["total"]) == 1_000_000
        # Each invoice_line carries its per-line net via bind-param ``:net``.
        line_rows = session.rows("invoice_line")
        assert len(line_rows) == 2
        nets = sorted(int(r["net"]) for r in line_rows)
        assert nets == [400_000, 600_000]

    async def test_rejects_quantity_zero(self) -> None:
        """``qty <= 0`` raises ValidationError.

        Pins the ``<= -> <`` mutant on line 137: mutated code would let
        ``qty = 0`` slip through the gate.
        """
        session = FakeAsyncSession()
        ctx = _build_ctx()
        with pytest.raises(ValidationError) as exc_info:
            await create_invoice(
                session,
                ctx,
                unit_id=uuid4(),
                participant_id=uuid4(),
                lines=[
                    {
                        "description": "tuition",
                        "quantity": 0,
                        "unit_price": 600_000,
                    }
                ],
            )
        assert "quantity must be positive" in str(exc_info.value)

    async def test_rejects_quantity_negative(self) -> None:
        """Same guard with a negative qty — complements the zero case."""
        session = FakeAsyncSession()
        ctx = _build_ctx()
        with pytest.raises(ValidationError):
            await create_invoice(
                session,
                ctx,
                unit_id=uuid4(),
                participant_id=uuid4(),
                lines=[
                    {
                        "description": "tuition",
                        "quantity": -1,
                        "unit_price": 600_000,
                    }
                ],
            )

    async def test_rejects_negative_discount(self) -> None:
        """``discount < 0`` raises ValidationError.

        Pins the ``< -> <=`` mutant on line 141: mutated code would let
        ``discount = 0`` slip through. The V13 test exercises the
        discount=0 success case.
        """
        session = FakeAsyncSession()
        ctx = _build_ctx()
        with pytest.raises(ValidationError) as exc_info:
            await create_invoice(
                session,
                ctx,
                unit_id=uuid4(),
                participant_id=uuid4(),
                lines=[
                    {
                        "description": "tuition",
                        "quantity": 1,
                        "unit_price": 600_000,
                        "discount_amount": -1,
                    }
                ],
            )
        assert "discount_amount must be non-negative" in str(exc_info.value)

    async def test_rejects_negative_net(self) -> None:
        """``net = qty*price - discount < 0`` raises ValidationError.

        qty=1, price=100, discount=200 -> net = -100.
        """
        session = FakeAsyncSession()
        ctx = _build_ctx()
        with pytest.raises(ValidationError) as exc_info:
            await create_invoice(
                session,
                ctx,
                unit_id=uuid4(),
                participant_id=uuid4(),
                lines=[
                    {
                        "description": "tuition",
                        "quantity": 1,
                        "unit_price": 100,
                        "discount_amount": 200,
                    }
                ],
            )
        assert "net_amount must be non-negative" in str(exc_info.value)

    async def test_installment_sum_must_equal_total_r12(self) -> None:
        """R12: sum(installment.due_amount) must equal invoice.total_amount.

        Installments sum to 900k but invoice total is 1M -> ValidationError.

        Pins the ``!= -> ==`` mutant on line 150: mutated code would let
        the 900k != 1M case pass.
        """
        session = FakeAsyncSession()
        ctx = _build_ctx()
        with pytest.raises(ValidationError) as exc_info:
            await create_invoice(
                session,
                ctx,
                unit_id=uuid4(),
                participant_id=uuid4(),
                lines=[
                    {
                        "description": "tuition",
                        "quantity": 1,
                        "unit_price": 1_000_000,
                    }
                ],
                installments=[
                    {
                        "sequence_no": 1,
                        "due_date": date(2025, 1, 1),
                        "due_amount": 500_000,
                    },
                    {
                        "sequence_no": 2,
                        "due_date": date(2025, 2, 1),
                        "due_amount": 400_000,
                    },
                ],
            )
        assert "R12" in str(exc_info.value)


# ===========================================================================
# void_invoice
# ===========================================================================


class TestVoidInvoice:
    """``void_invoice`` — V18: reject when active allocations exist."""

    async def test_void_with_active_allocations_rejected(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """V18: void must raise StateConflictError when allocations exist.

        ``InvoiceRepository.sum_active_allocations`` is patched to return
        1; ``1 > 0`` triggers the StateConflictError branch on line 256.

        Pins the ``0 -> 1`` mutant on line 256: mutated ``allocated > 1``
        would let ``allocated = 1`` slip through.
        """
        session = FakeAsyncSession()
        tenant_id = uuid4()
        ctx = _build_ctx(tenant_id=tenant_id)
        invoice_id = _seed_invoice(session, tenant_id)

        async def _return_one(self: Any, iid: UUID) -> int:
            return 1

        monkeypatch.setattr(
            InvoiceRepository, "sum_active_allocations", _return_one
        )
        with pytest.raises(StateConflictError) as exc_info:
            await void_invoice(
                session, ctx, invoice_id=invoice_id, reason="void"
            )
        assert "allocations" in str(exc_info.value).lower()

    async def test_void_with_zero_allocations_succeeds(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """No active allocations -> void transitions to VOID status.

        Pins the ``> -> >=`` mutant on line 256: mutated ``allocated >= 0``
        would raise even when ``allocated = 0``.
        """
        session = FakeAsyncSession()
        tenant_id = uuid4()
        ctx = _build_ctx(tenant_id=tenant_id)
        invoice_id = _seed_invoice(session, tenant_id)

        async def _return_zero(self: Any, iid: UUID) -> int:
            return 0

        monkeypatch.setattr(
            InvoiceRepository, "sum_active_allocations", _return_zero
        )
        await void_invoice(session, ctx, invoice_id=invoice_id, reason="void")
        # The UPDATE invoice SET document_status = 'void' was issued.
        invoice_rows = session.rows("invoice")
        assert len(invoice_rows) == 1
        assert invoice_rows[0]["document_status"] == InvoiceDocumentStatus.VOID.value


# ===========================================================================
# allocate_funds
# ===========================================================================


class TestAllocateFunds:
    """``allocate_funds`` — V15 capacity + V16 idempotency."""

    async def test_v15_allocation_exceeds_available_raises(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """V15: amount > available raises InsufficientFundsError.

        recognized=100k, no prior allocations, requested=200k
        -> available = 100k; ``100k < 200k`` triggers InsufficientFundsError.

        Pins the ``0 -> 1`` mutant on line 444: mutated ``available < 1``
        would let ``100k < 1`` slip through (no raise).
        """
        session = FakeAsyncSession()
        tenant_id = uuid4()
        ctx = _build_ctx(tenant_id=tenant_id)
        vp_id, inst_id = _seed_vp_inst(session, tenant_id, recognized=100_000)

        async def _return_zero(self: Any, pid: UUID) -> int:
            return 0

        monkeypatch.setattr(
            VerifiedPaymentRepository,
            "sum_active_allocations",
            _return_zero,
        )
        with pytest.raises(InsufficientFundsError) as exc_info:
            await allocate_funds(
                session,
                ctx,
                payment_id=vp_id,
                installment_id=inst_id,
                amount=200_000,
                idempotency_id=uuid4(),
            )
        # Error details pin the available vs. requested amounts.
        assert exc_info.value.details.get("available") == 100_000
        assert exc_info.value.details.get("requested") == 200_000

    async def test_v16_same_idempotency_id_returns_existing(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """V16: re-allocation with the same idempotency_id returns the existing row.

        A pre-seeded ``payment_allocation`` row matching the
        ``(tenant_id, allocated_by, idempotency_id)`` tuple is returned
        as-is; no new allocation is inserted and ``issue_receipt`` is
        suppressed (``receipt`` comes back ``None``).
        """
        session = FakeAsyncSession()
        tenant_id = uuid4()
        account_id = uuid4()
        ctx = _build_ctx(tenant_id=tenant_id, account_id=account_id)
        vp_id, inst_id = _seed_vp_inst(session, tenant_id)
        idem_id = uuid4()
        existing_alloc_id = uuid4()
        # Seed an existing allocation row that satisfies all three SELECTs
        # the function performs against payment_allocation + the credit
        # SELECT against financial_adjustment. ``total = 0`` makes the
        # no-aggregate SUM=0 the fake returns.
        session.seed(
            "payment_allocation",
            {
                "id": existing_alloc_id,
                "tenant_id": tenant_id,
                "payment_id": vp_id,
                "installment_id": inst_id,
                "amount": 50_000,
                "allocated_at": None,
                "allocated_by": str(account_id),
                "idempotency_id": str(idem_id),
                "total": 0,
            },
        )
        session.seed(
            "financial_adjustment",
            {
                "id": uuid4(),
                "tenant_id": tenant_id,
                "installment_id": inst_id,
                "adjustment_kind": "invoice_credit",
                "amount": 0,
                "total": 0,
            },
        )

        async def _return_zero(self: Any, pid: UUID) -> int:
            return 0

        monkeypatch.setattr(
            VerifiedPaymentRepository,
            "sum_active_allocations",
            _return_zero,
        )
        alloc, receipt = await allocate_funds(
            session,
            ctx,
            payment_id=vp_id,
            installment_id=inst_id,
            amount=75_000,
            idempotency_id=idem_id,
        )
        # The returned allocation is the existing one.
        assert alloc.id == existing_alloc_id
        assert receipt is None
        # Only the seeded allocation row exists — no new INSERT.
        all_allocs = session.rows("payment_allocation")
        assert len(all_allocs) == 1