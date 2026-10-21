"""Unit tests for ``ksma.finance.application`` — invoice + allocation seam.

Pins ONE behavior per test class (issue #43, P5.2.7):

- ``TestCreateInvoice`` / ``TestCreateInvoiceLineMath``: line math (V13 —
  600k+400k=1M), quantity/price/discount/net validation guards *and their
  legal edges* (absent discount key, zero price, zero net), R12
  installment-sum equality, 1-based ``line_no``.
- ``TestVoidInvoice``: V18 — void rejected when active allocations exist.
- ``TestAllocateFunds`` / ``TestAllocateFundsBoundaries``: V15 capacity,
  V16 lock-ordered idempotency, V17 same-key = one effect, and the
  available / remainder boundaries in *both* directions (a rejection that
  pins the exact figure in ``details``, plus an acceptance at the boundary).
- ``TestPaymentSubmissionAndVerification``: V14 recognised-vs-claimed delta.
- ``TestReversals``: R15 reasonless-reversal rejection and the summed
  ``reversal_total``.
- ``TestInvoiceCredit``: the credit capacity guard on the installment
  remainder.
- ``TestComputeInvoiceProjection*``: per-installment effective/remainder
  arithmetic, the ``due_status`` boundary table, and the
  ``settled_zero``/``paid``/``partial``/``unpaid`` dispatch.
- ``TestRepository*``: the ``next_invoice_no`` / ``next_receipt_no``
  sequences and both ``sum_active_allocations`` capacity reads.

Persistence has two seams. The pre-existing classes use the in-memory
``FakeAsyncSession`` from ``tests/unit/conftest_helpers.py``; the
policy-version SELECT is monkey-patched at the module level so we never
need to seed a ``policy_version`` row. Repository helpers whose SQL joins
aliased columns (``InvoiceRepository.sum_active_allocations``) are also
patched at the class level — the fake's WHERE filter does not understand
table aliases.

Ticket #22 adds a second, narrower double: ``_FakeSession``, which
replays SELECT results from a FIFO, one entry per ``execute()``, in the
order the service issues them. The dict-table fake cannot express the two
shapes the finance services lean on hardest — table-qualified aggregate
predicates (``pa.installment_id = :iid``) whose value only exists as a
``COALESCE(SUM(...))`` alias, and the projection query whose result the
application iterates directly (``for r in rows``). Writes are recorded,
not interpreted: the assertions read the returned domain objects and the
``details`` payload of the raised error, not the table state.
"""
from __future__ import annotations

from collections import deque
from datetime import date, timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest

from ksma.finance import application as fin_app
from ksma.finance.application import (
    allocate_funds,
    compute_invoice_projection,
    create_invoice,
    invoice_credit,
    reverse_allocation,
    reverse_payment,
    submit_payment,
    verify_payment,
    void_invoice,
)
from ksma.finance.domain import (
    FinancialAdjustmentKind,
    InvoiceDocumentStatus,
    PaymentAllocation,
    PaymentSubmissionStatus,
)
from ksma.finance.infrastructure.repositories import (
    InvoiceRepository,
    PaymentAllocationRepository,
    ReceiptRepository,
    VerifiedPaymentRepository,
)
from ksma.shared.errors import (
    InsufficientFundsError,
    StateConflictError,
    ValidationError,
)
from ksma.shared.rbac import RoleCode
from ksma.shared.time import clock
from tests.unit.conftest_helpers import FakeAsyncSession, FakeTenantContext

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


# ===========================================================================
# Scripted session (ticket #22 — T18's `_FakeSession` shape)
# ===========================================================================
#
# The dict-table ``FakeAsyncSession`` cannot express the two shapes the
# finance services lean on hardest:
#
# 1. aggregate SELECTs the fake's WHERE parser cannot match, because the
#    predicates are table-qualified (``pa.installment_id = :iid``) and the
#    projected value only exists as a bind-free ``COALESCE(SUM(...))`` alias;
# 2. the projection query, whose ``FakeResult`` the application iterates
#    directly (``for r in rows``) rather than through ``.all()``.
#
# ``_FakeSession`` therefore replays SELECT results from a FIFO, one entry
# per ``execute()``, in the exact order the service issues them. Every entry
# is a plain list of ``_Row`` mappings, so a query that is *supposed* to find
# nothing is an explicit ``[]``. Writes (INSERT/UPDATE) are recorded, not
# interpreted — the domain assertions read the returned domain objects, not
# the table state.
#
# When the FIFO runs dry a zeroed aggregate row is returned, so an
# un-scripted ``next_*_no()`` yields sequence 1 rather than blowing up.


class _Row(dict):
    """Dict with attribute access — the shape the ``_to_*`` mappers expect."""

    __getattr__ = dict.__getitem__


class _ScriptedResult:
    """``AsyncSession.execute`` result over a pre-scripted row list."""

    def __init__(self, rows: list[_Row]) -> None:
        self._rows = list(rows)

    def __iter__(self):
        return iter(self._rows)

    def first(self) -> _Row | None:
        return self._rows[0] if self._rows else None

    def all(self) -> list[_Row]:
        return list(self._rows)

    def one_or_none(self) -> _Row | None:
        return self.first()

    def scalar(self) -> Any:
        row = self.first()
        return None if row is None else next(iter(row.values()))

    def scalar_one(self) -> Any:
        row = self.first()
        if row is None:
            raise AssertionError("scalar_one() on an empty scripted result")
        return next(iter(row.values()))

    def scalars(self) -> list[Any]:
        return [next(iter(r.values())) for r in self._rows]


_ZERO_AGGREGATE = _Row({"n": 0, "total": 0})


class _FakeSession:
    """Replays SELECT results from a FIFO; records writes for inspection."""

    def __init__(self, *selects: list[_Row]) -> None:
        self._selects: deque[list[_Row]] = deque(selects)
        self.statements: list[tuple[str, dict[str, Any]]] = []
        self.inserts: list[dict[str, Any]] = []
        self.updates: list[dict[str, Any]] = []

    async def execute(self, statement: Any, parameters: Any = None) -> _ScriptedResult:
        sql = str(getattr(statement, "text", statement))
        params = dict(parameters or {})
        self.statements.append((sql, params))
        verb = sql.strip().split(None, 1)[0].upper() if sql.strip() else ""
        if verb == "SELECT":
            rows = self._selects.popleft() if self._selects else [_ZERO_AGGREGATE]
            return _ScriptedResult(rows)
        if verb == "INSERT":
            self.inserts.append(params)
        elif verb == "UPDATE":
            self.updates.append(params)
        return _ScriptedResult([])

    async def flush(self) -> None:
        return None

    async def commit(self) -> None:
        return None

    async def rollback(self) -> None:
        return None

    async def refresh(self, instance: Any) -> None:
        return None

    async def add(self, instance: Any) -> None:
        return None


def _agg(total: int | None) -> list[_Row]:
    """One aggregate row: ``SELECT COALESCE(SUM(x), 0) AS total …``."""
    return [_Row({"total": total})]


def _seq(n: int | None) -> list[_Row]:
    """One sequence row: ``SELECT COALESCE(MAX(...), 0) AS n …``."""
    return [_Row({"n": n})]


def _invoice_row(
    tenant_id: UUID,
    *,
    invoice_id: UUID | None = None,
    total: int = 1_000_000,
    status: InvoiceDocumentStatus = InvoiceDocumentStatus.ISSUED,
) -> _Row:
    """A row carrying every column ``_to_invoice`` reads."""
    return _Row(
        {
            "id": invoice_id or uuid4(),
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
        }
    )


def _installment_row(
    tenant_id: UUID,
    *,
    installment_id: UUID | None = None,
    invoice_id: UUID | None = None,
    due_amount: int = 1_000_000,
    due_date: date | None = None,
) -> _Row:
    """A row carrying every column ``_to_installment`` reads."""
    return _Row(
        {
            "id": installment_id or uuid4(),
            "tenant_id": tenant_id,
            "invoice_id": invoice_id or uuid4(),
            "sequence_no": 1,
            "due_date": due_date or date(2025, 1, 1),
            "due_amount": due_amount,
            "timezone": "Asia/Jakarta",
            "created_at": None,
            "created_by": "test",
        }
    )


def _verified_row(
    tenant_id: UUID,
    *,
    payment_id: UUID | None = None,
    recognized: int = 1_000_000,
    reversal_total: int = 0,
) -> _Row:
    """A row carrying every column ``_to_verified`` reads."""
    return _Row(
        {
            "id": payment_id or uuid4(),
            "tenant_id": tenant_id,
            "unit_id": uuid4(),
            "submission_id": uuid4(),
            "participant_id": uuid4(),
            "recognized_amount": recognized,
            "currency": "IDR",
            "verification": {"actor": "test"},
            "received_date": date(2025, 1, 1),
            "reversal_total": reversal_total,
            "created_at": None,
            "created_by": "test",
        }
    )


def _submission_row(
    tenant_id: UUID,
    *,
    submission_id: UUID | None = None,
    claimed_amount: int = 1_000_000,
    status: PaymentSubmissionStatus = PaymentSubmissionStatus.SUBMITTED,
) -> _Row:
    """A row carrying every column ``_to_submission`` reads."""
    return _Row(
        {
            "id": submission_id or uuid4(),
            "tenant_id": tenant_id,
            "unit_id": uuid4(),
            "participant_id": uuid4(),
            "invoice_id": None,
            "submitted_by": "test",
            "claimed_amount": claimed_amount,
            "currency": "IDR",
            "transfer_at": None,
            "proof_attachment_id": None,
            "transfer_reference": None,
            "status": status.value,
            "review_decision": None,
            "created_at": None,
            "updated_at": None,
            "created_by": "test",
            "updated_by": "test",
        }
    )


def _alloc_script(
    tenant_id: UUID,
    *,
    recognized: int = 1_000_000,
    reversal_total: int = 0,
    used: int = 0,
    due_amount: int = 1_000_000,
    installment_used: int | None = 0,
    invoice_credit: int | None = 0,
    existing_allocation: bool = False,
) -> tuple[_FakeSession, UUID, UUID]:
    """Script the six SELECTs ``allocate_funds`` issues, in order.

    ``sum_active_allocations`` is patched to ``used`` by the caller, so the
    FIFO holds: verified payment, installment, installment-used SUM,
    invoice-credit SUM, idempotency replay (empty unless a row is requested),
    and finally the receipt sequence.
    """
    payment_id, installment_id = uuid4(), uuid4()
    session = _FakeSession(
        [_verified_row(tenant_id, payment_id=payment_id, recognized=recognized,
                       reversal_total=reversal_total)],
        [_installment_row(tenant_id, installment_id=installment_id, due_amount=due_amount)],
        _agg(installment_used),
        _agg(invoice_credit),
        [_Row({"id": uuid4(), "amount": 1})] if existing_allocation else [],
        _seq(4),
    )
    return session, payment_id, installment_id


def _patch_used(monkeypatch: pytest.MonkeyPatch, used: int) -> None:
    """Pin ``VerifiedPaymentRepository.sum_active_allocations`` to ``used``."""

    async def _used(self: Any, payment_id: UUID) -> int:
        return used

    monkeypatch.setattr(VerifiedPaymentRepository, "sum_active_allocations", _used)


# ===========================================================================
# create_invoice — line-math boundaries (V13 / R12)
# ===========================================================================


class TestCreateInvoiceLineMath:
    """``create_invoice`` boundary table over the per-line math guards."""

    @pytest.mark.parametrize(
        ("line", "expected_total"),
        [
            # No ``discount_amount`` key at all -> the default must be 0.
            pytest.param(
                {"description": "tuition", "quantity": 1, "unit_price": 100},
                100,
                id="discount_key_absent",
            ),
            # ``unit_price == 0`` is legal: only negatives are rejected.
            pytest.param(
                {"description": "free", "quantity": 1, "unit_price": 0},
                0,
                id="unit_price_zero",
            ),
            # ``net == 0`` is legal: the guard is ``net < 0``, not ``net <= 0``.
            pytest.param(
                {
                    "description": "fully discounted",
                    "quantity": 1,
                    "unit_price": 100,
                    "discount_amount": 100,
                },
                0,
                id="net_zero",
            ),
            # A discount actually subtracts.
            pytest.param(
                {
                    "description": "sibling discount",
                    "quantity": 1,
                    "unit_price": 1_000,
                    "discount_amount": 200,
                },
                800,
                id="discount_subtracts",
            ),
        ],
    )
    async def test_line_math_boundaries(self, line: dict[str, Any], expected_total: int) -> None:
        """Each legal edge is accepted and lands on the exact computed total.

        Pins ``qty*price - disc`` on both the validation loop (line 143) and
        the INSERT-side recomputation (line 172) for every boundary the four
        guards draw: absent discount, zero price, zero net, real discount.
        """
        session = FakeAsyncSession()
        ctx = _build_ctx()
        inv = await create_invoice(
            session,
            ctx,
            unit_id=uuid4(),
            participant_id=uuid4(),
            lines=[line],
        )
        assert inv.total_amount == expected_total
        line_rows = session.rows("invoice_line")
        assert len(line_rows) == 1
        assert int(line_rows[0]["net"]) == expected_total
        assert int(line_rows[0]["disc"]) == int(line.get("discount_amount", 0))

    async def test_line_numbers_start_at_one(self) -> None:
        """``enumerate(lines, start=1)`` — the first line is ``line_no = 1``.

        Pins ``start=1 -> 0`` on line 171.
        """
        session = FakeAsyncSession()
        ctx = _build_ctx()
        await create_invoice(
            session,
            ctx,
            unit_id=uuid4(),
            participant_id=uuid4(),
            lines=[
                {"description": "tuition", "quantity": 1, "unit_price": 600_000},
                {"description": "books", "quantity": 1, "unit_price": 400_000},
            ],
        )
        assert [int(r["ln"]) for r in session.rows("invoice_line")] == [1, 2]


# ===========================================================================
# submit_payment / verify_payment (V14)
# ===========================================================================


class TestPaymentSubmissionAndVerification:
    """Claimed-vs-recognised boundary and the V14 delta marker."""

    @pytest.mark.parametrize("claimed", [0, -1], ids=["claimed_zero", "claimed_negative"])
    async def test_submit_payment_rejects_non_positive_claim(self, claimed: int) -> None:
        """``claimed_amount <= 0`` is rejected — zero included.

        Pins ``<= -> <`` and ``<= 0 -> <= 1`` on line 288: a mutated guard
        accepts the zero claim, which would let an empty submission through.
        """
        ctx = _build_ctx()
        session = _FakeSession()
        with pytest.raises(ValidationError, match="claimed_amount must be positive"):
            await submit_payment(
                session,
                ctx,
                unit_id=uuid4(),
                participant_id=uuid4(),
                invoice_id=None,
                claimed_amount=claimed,
                transfer_at=None,
                proof_attachment_id=None,
                transfer_reference=None,
            )

    async def test_submit_payment_accepts_one_rupiah_claim(self) -> None:
        """A claim of exactly 1 is legal — the guard rejects ``<= 0``, not ``<= 1``.

        Pins the ``0 -> 1`` mutant on line 288, which would reject the
        smallest possible claim outright.
        """
        session = _FakeSession()
        ctx = _build_ctx()
        submission = await submit_payment(
            session,
            ctx,
            unit_id=uuid4(),
            participant_id=uuid4(),
            invoice_id=None,
            claimed_amount=1,
            transfer_at=None,
            proof_attachment_id=None,
            transfer_reference=None,
        )
        assert submission.claimed_amount == 1

    async def test_submit_payment_accepts_positive_claim(self) -> None:
        """Positive assertion: the submitted claim round-trips unchanged."""
        session = _FakeSession()
        ctx = _build_ctx()
        submission = await submit_payment(
            session,
            ctx,
            unit_id=uuid4(),
            participant_id=uuid4(),
            invoice_id=None,
            claimed_amount=250_000,
            transfer_at=None,
            proof_attachment_id=None,
            transfer_reference="TRF-1",
        )
        assert submission.claimed_amount == 250_000
        assert submission.status is PaymentSubmissionStatus.SUBMITTED

    @pytest.mark.parametrize("recognized", [0, -1], ids=["recognized_zero", "recognized_negative"])
    async def test_verify_payment_rejects_non_positive_recognition(
        self, recognized: int
    ) -> None:
        """``recognized_amount <= 0`` is rejected — zero included.

        Pins ``<= -> <`` and ``<= 0 -> <= 1`` on line 321.
        """
        session = _FakeSession()
        ctx = _build_ctx()
        with pytest.raises(ValidationError, match="recognized_amount must be positive"):
            await verify_payment(
                session,
                ctx,
                submission_id=uuid4(),
                recognized_amount=recognized,
                reason="r",
            )

    async def test_verify_payment_accepts_one_rupiah_recognition(self) -> None:
        """A recognition of exactly 1 is legal — the guard rejects ``<= 0``.

        Pins the ``0 -> 1`` mutant on line 321.
        """
        tenant_id = uuid4()
        ctx = _build_ctx(tenant_id=tenant_id)
        submission = _submission_row(tenant_id, claimed_amount=1)
        session = _FakeSession([submission])
        vp = await verify_payment(
            session,
            ctx,
            submission_id=submission["id"],
            recognized_amount=1,
            reason="review",
        )
        assert vp.recognized_amount == 1

    @pytest.mark.parametrize(
        ("claimed", "recognized", "expect_delta"),
        [
            pytest.param(1_000_000, 900_000, True, id="recognised_below_claim"),
            pytest.param(1_000_000, 1_000_000, False, id="recognised_equals_claim"),
            pytest.param(1_000_000, 1_100_000, True, id="recognised_above_claim"),
        ],
    )
    async def test_v14_delta_reason_marks_recognised_claim_mismatch(
        self, claimed: int, recognized: int, expect_delta: bool
    ) -> None:
        """V14: the ``delta_reason`` marker is set iff recognition != claim.

        Pins ``!= -> ==`` on line 345 — the mutated comparison records the
        delta on a clean match and drops it on a genuine mismatch, which is
        exactly the audit signal the pilot review queue reads.
        """
        tenant_id = uuid4()
        ctx = _build_ctx(tenant_id=tenant_id)
        submission = _submission_row(tenant_id, claimed_amount=claimed)
        session = _FakeSession([submission])
        vp = await verify_payment(
            session,
            ctx,
            submission_id=submission["id"],
            recognized_amount=recognized,
            reason="review",
        )
        assert int(vp.recognized_amount) == recognized
        delta = vp.verification["delta_reason"]
        if expect_delta:
            assert delta is not None and delta != ""
        else:
            assert delta is None


# ===========================================================================
# allocate_funds — V15 / V16 boundaries
# ===========================================================================


class TestAllocateFundsBoundaries:
    """``allocate_funds`` — available / remainder boundaries, both directions."""

    @pytest.mark.parametrize("amount", [0, -1], ids=["amount_zero", "amount_negative"])
    async def test_rejects_non_positive_amount(self, amount: int) -> None:
        """``amount <= 0`` is rejected — zero included.

        Pins ``<= -> <`` and ``<= 0 -> <= 1`` on line 424.
        """
        session = _FakeSession()
        ctx = _build_ctx()
        with pytest.raises(ValidationError, match="amount must be positive"):
            await allocate_funds(
                session,
                ctx,
                payment_id=uuid4(),
                installment_id=uuid4(),
                amount=amount,
                idempotency_id=uuid4(),
            )

    async def test_accepts_one_rupiah_allocation(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An allocation of exactly 1 is legal — the guard rejects ``<= 0``.

        Pins the ``0 -> 1`` mutant on line 424, which would reject the
        smallest possible allocation outright.
        """
        tenant_id = uuid4()
        ctx = _build_ctx(tenant_id=tenant_id)
        session, payment_id, installment_id = _alloc_script(
            tenant_id, recognized=1_000, due_amount=1_000
        )
        _patch_used(monkeypatch, 0)
        alloc, _receipt = await allocate_funds(
            session,
            ctx,
            payment_id=payment_id,
            installment_id=installment_id,
            amount=1,
            idempotency_id=uuid4(),
        )
        assert alloc.amount == 1

    async def test_available_boundary_exact_amount_succeeds(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """V15 off-boundary: allocating the exact available amount is legal.

        recognized=100k, no reversal, no prior use -> available == 100k and
        the request for 100k must commit. Pins ``available < amount`` ->
        ``available <= amount`` on line 444, and pins the ``issue_receipt:
        bool = True`` default (line 414) by asserting a receipt is issued.
        """
        tenant_id = uuid4()
        ctx = _build_ctx(tenant_id=tenant_id)
        session, payment_id, installment_id = _alloc_script(
            tenant_id, recognized=100_000, due_amount=100_000
        )
        _patch_used(monkeypatch, 0)
        alloc, receipt = await allocate_funds(
            session,
            ctx,
            payment_id=payment_id,
            installment_id=installment_id,
            amount=100_000,
            idempotency_id=uuid4(),
        )
        assert alloc.amount == 100_000
        assert receipt is not None
        assert receipt.receipt_no == "RCP-000005"

    async def test_available_subtracts_reversal_and_prior_use(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """V14: available = recognized − reversal_total − sum(active).

        recognized=100k, reversal=30k, prior use=20k -> available == 50k, so
        a 60k request is rejected and the error carries exactly 50k.
        Pins both ``- -> +`` mutants on line 443.
        """
        tenant_id = uuid4()
        ctx = _build_ctx(tenant_id=tenant_id)
        session, payment_id, installment_id = _alloc_script(
            tenant_id, recognized=100_000, reversal_total=30_000, due_amount=1_000_000
        )
        _patch_used(monkeypatch, 20_000)
        with pytest.raises(InsufficientFundsError) as exc_info:
            await allocate_funds(
                session,
                ctx,
                payment_id=payment_id,
                installment_id=installment_id,
                amount=60_000,
                idempotency_id=uuid4(),
            )
        assert exc_info.value.details["available"] == 50_000
        assert exc_info.value.details["requested"] == 60_000

    async def test_remainder_rejection_pins_exact_remainder(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """R12: remainder = due − sum(active allocations) − invoice_credit.

        due=1M, allocated=200k, credited=100k -> remainder == 700k. A 750k
        request must be rejected *and* report 700k, which pins both
        ``- -> +`` mutants on line 477. The exact figure in ``details`` also
        pins the ``or -> and`` mutants (mutated remainder is larger, so the
        raise never fires).
        """
        tenant_id = uuid4()
        ctx = _build_ctx(tenant_id=tenant_id)
        session, payment_id, installment_id = _alloc_script(
            tenant_id,
            due_amount=1_000_000,
            installment_used=200_000,
            invoice_credit=100_000,
        )
        _patch_used(monkeypatch, 0)
        with pytest.raises(InsufficientFundsError) as exc_info:
            await allocate_funds(
                session,
                ctx,
                payment_id=payment_id,
                installment_id=installment_id,
                amount=750_000,
                idempotency_id=uuid4(),
            )
        assert exc_info.value.details["remainder"] == 700_000

    async def test_remainder_boundary_exact_amount_succeeds(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Off-boundary: allocating exactly the remainder commits.

        Pins the ``or 0 -> or 1`` mutants on line 477 — the mutated
        remainder is one short of 700k, so the exact request raises.
        """
        tenant_id = uuid4()
        ctx = _build_ctx(tenant_id=tenant_id)
        session, payment_id, installment_id = _alloc_script(
            tenant_id,
            due_amount=1_000_000,
            installment_used=200_000,
            invoice_credit=100_000,
        )
        _patch_used(monkeypatch, 0)
        alloc, _receipt = await allocate_funds(
            session,
            ctx,
            payment_id=payment_id,
            installment_id=installment_id,
            amount=700_000,
            idempotency_id=uuid4(),
        )
        assert alloc.amount == 700_000

    async def test_v17_same_key_returns_the_one_existing_allocation(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """V17/R21: replaying an idempotency key is one effect, not two.

        The idempotency SELECT finds a row, so the service returns that
        allocation instead of inserting a second one — and skips the
        receipt.
        """
        tenant_id = uuid4()
        ctx = _build_ctx(tenant_id=tenant_id)
        session, payment_id, installment_id = _alloc_script(
            tenant_id, existing_allocation=True
        )
        _patch_used(monkeypatch, 0)
        replayed_id = uuid4()

        async def _get_existing(self: Any, allocation_id: UUID) -> Any:
            return PaymentAllocation(
                id=replayed_id,
                tenant_id=tenant_id,
                payment_id=payment_id,
                installment_id=installment_id,
                amount=42_000,
                allocated_by="test",
                idempotency_id=uuid4(),
            )

        # The replay path calls ``PaymentAllocationRepository.get``; patch the
        # mapper-level lookup so the assertion reads a real PaymentAllocation.
        monkeypatch.setattr(
            PaymentAllocationRepository, "get", _get_existing, raising=True
        )
        alloc, receipt = await allocate_funds(
            session,
            ctx,
            payment_id=payment_id,
            installment_id=installment_id,
            amount=42_000,
            idempotency_id=uuid4(),
        )
        assert alloc.id == replayed_id
        assert receipt is None
        # One effect only — no INSERT of a second allocation.
        assert session.inserts == []


# ===========================================================================
# Reversals (V18)
# ===========================================================================


class TestReversals:
    """R15 — reversals are refused without an audit reason."""

    @pytest.mark.parametrize("reason", ["", None], ids=["empty_reason", "missing_reason"])
    async def test_reverse_allocation_requires_a_reason(self, reason: Any) -> None:
        """``if not reason`` — the guard is falsiness, not ``reason == ""``.

        Pins ``delete not`` on line 571.
        """
        session = _FakeSession()
        ctx = _build_ctx()
        with pytest.raises(ValidationError, match="reason is required"):
            await reverse_allocation(
                session, ctx, allocation_id=uuid4(), reason=reason
            )
        assert session.statements == []

    @pytest.mark.parametrize("reason", ["", None], ids=["empty_reason", "missing_reason"])
    async def test_reverse_payment_requires_a_reason(self, reason: Any) -> None:
        """Pins ``delete not`` on line 606.

        The guard must fire before the payment is even locked, so a
        reasonless reversal never mutates ``reversal_total``.
        """
        session = _FakeSession()
        ctx = _build_ctx()
        with pytest.raises(ValidationError, match="reason is required"):
            await reverse_payment(session, ctx, payment_id=uuid4(), reason=reason)
        assert session.statements == []

    async def test_reverse_payment_totals_the_reversed_allocations(self) -> None:
        """R15: ``reversal_total`` is the sum of the allocations it reversed.

        One active allocation of 100k -> the payment-reversal adjustment
        carries exactly 100k, not 100k+1. Pins ``total_reversed = 0 -> 1``
        on line 629.
        """
        tenant_id = uuid4()
        ctx = _build_ctx(tenant_id=tenant_id)
        payment_id = uuid4()
        allocation_id = uuid4()
        session = _FakeSession(
            [_verified_row(tenant_id, payment_id=payment_id)],
            [_Row({"id": allocation_id, "amount": 100_000})],
        )
        adjustment = await reverse_payment(
            session, ctx, payment_id=payment_id, reason="bounced"
        )
        assert adjustment.amount == 100_000
        assert adjustment.adjustment_kind is FinancialAdjustmentKind.PAYMENT_REVERSAL
        # Two adjustments were written: the per-allocation reversal (100k)
        # and the payment-level total. The outbox row is not an adjustment.
        adjustments = [p for p in session.inserts if "ak" in p]
        assert [p["ak"] for p in adjustments] == [
            FinancialAdjustmentKind.ALLOCATION_REVERSAL.value,
            FinancialAdjustmentKind.PAYMENT_REVERSAL.value,
        ]
        assert [int(p["amt"]) for p in adjustments] == [100_000, 100_000]
        # reversal_total was bumped by the summed amount, not by the guard.
        assert any(p.get("amt") == 100_000 for p in session.updates)


# ===========================================================================
# invoice_credit — V13 correction capacity
# ===========================================================================


class TestInvoiceCredit:
    """``invoice_credit`` — the credit may not exceed the installment remainder."""

    @pytest.mark.parametrize("amount", [0, -1], ids=["credit_zero", "credit_negative"])
    async def test_rejects_non_positive_credit(self, amount: int) -> None:
        """``amount <= 0`` is rejected — zero included.

        Pins ``<= -> <`` and ``<= 0 -> <= 1`` on line 677.
        """
        session = _FakeSession()
        ctx = _build_ctx()
        with pytest.raises(ValidationError, match="amount must be positive"):
            await invoice_credit(
                session, ctx, installment_id=uuid4(), amount=amount, reason="goodwill"
            )

    async def test_accepts_one_rupiah_credit(self) -> None:
        """A credit of exactly 1 is legal — the guard rejects ``<= 0``.

        Pins the ``0 -> 1`` mutant on line 677.
        """
        tenant_id = uuid4()
        ctx = _build_ctx(tenant_id=tenant_id)
        installment_id = uuid4()
        session = _FakeSession(
            [_installment_row(tenant_id, installment_id=installment_id, due_amount=1_000)],
            _agg(0),
            _agg(0),
        )
        adjustment = await invoice_credit(
            session, ctx, installment_id=installment_id, amount=1, reason="goodwill"
        )
        assert adjustment.amount == 1

    async def test_credit_rejection_pins_exact_remainder(self) -> None:
        """The credit guard reads the same remainder as ``allocate_funds``.

        due=1M, allocated=200k, credited=100k -> remainder == 700k; a 750k
        credit is rejected and the error reports 700k. Pins both
        ``- -> +`` mutants, both ``or -> and`` mutants and both
        ``or 0 -> or 1`` mutants on line 712.
        """
        tenant_id = uuid4()
        ctx = _build_ctx(tenant_id=tenant_id)
        installment_id = uuid4()
        session = _FakeSession(
            [_installment_row(tenant_id, installment_id=installment_id, due_amount=1_000_000)],
            _agg(200_000),
            _agg(100_000),
        )
        with pytest.raises(ValidationError) as exc_info:
            await invoice_credit(
                session,
                ctx,
                installment_id=installment_id,
                amount=750_000,
                reason="goodwill",
            )
        assert exc_info.value.details["remainder"] == 700_000

    async def test_credit_at_exact_remainder_is_accepted(self) -> None:
        """Off-boundary: a credit exactly equal to the remainder commits.

        Pins ``> -> >=`` on line 713 — the mutated guard rejects the
        boundary credit. Also pins the ``or 0 -> or 1`` mutants on line 712,
        whose mutated remainder is one short of 700k.
        """
        tenant_id = uuid4()
        ctx = _build_ctx(tenant_id=tenant_id)
        installment_id = uuid4()
        session = _FakeSession(
            [_installment_row(tenant_id, installment_id=installment_id, due_amount=1_000_000)],
            _agg(200_000),
            _agg(100_000),
        )
        adjustment = await invoice_credit(
            session,
            ctx,
            installment_id=installment_id,
            amount=700_000,
            reason="goodwill",
        )
        assert adjustment.amount == 700_000
        assert adjustment.adjustment_kind is FinancialAdjustmentKind.INVOICE_CREDIT

    async def test_credit_at_exact_remainder_with_no_prior_activity_is_accepted(
        self,
    ) -> None:
        """With both aggregate reads empty, a credit may consume the whole due.

        The two SUMs are *falsy* here (0, not a populated row), which is
        exactly the shape the ``or 0 -> or 1`` mutants on line 712 expose:
        the mutated remainder is one short of 1M, so the exact-boundary
        credit would be rejected.
        """
        tenant_id = uuid4()
        ctx = _build_ctx(tenant_id=tenant_id)
        installment_id = uuid4()
        session = _FakeSession(
            [_installment_row(tenant_id, installment_id=installment_id, due_amount=1_000_000)],
            _agg(0),
            _agg(0),
        )
        adjustment = await invoice_credit(
            session,
            ctx,
            installment_id=installment_id,
            amount=1_000_000,
            reason="goodwill",
        )
        assert adjustment.amount == 1_000_000


# ===========================================================================
# compute_invoice_projection — V14 projection
# ===========================================================================


def _proj_row(
    tenant_id: UUID,
    *,
    due_date: date,
    due_amount: int,
    credited: int = 0,
    allocated: int = 0,
    installment_id: UUID | None = None,
) -> _Row:
    """One row of the installment projection query."""
    return _Row(
        {
            "id": installment_id or uuid4(),
            "tenant_id": tenant_id,
            "due_date": due_date,
            "due_amount": due_amount,
            "credited": credited,
            "allocated": allocated,
        }
    )


def _projection_session(
    tenant_id: UUID,
    *,
    total: int,
    allocated: int,
    rows: list[_Row],
    invoice_id: UUID | None = None,
) -> tuple[_FakeSession, UUID]:
    """Script the three SELECTs ``compute_invoice_projection`` issues.

    Order: the invoice row (via ``InvoiceRepository.get``), the
    active-allocation SUM, then the projection rows themselves.
    """
    session = _FakeSession(
        [_invoice_row(tenant_id, invoice_id=invoice_id, total=total)],
        _agg(allocated),
        rows,
    )
    return session, invoice_id or uuid4()


class TestComputeInvoiceProjectionRowMath:
    """Per-installment effective amount, remainder and overdue accumulation."""

    async def test_effective_amount_and_remainder_subtract_credit_and_allocation(
        self,
    ) -> None:
        """V14: effective = due − credited; remainder = effective − allocated.

        due=1M, credited=200k, allocated=100k on an already-overdue
        installment -> effective 800k, remainder 700k, all of it overdue.
        Pins both ``- -> +`` mutants on lines 766 / 767, the
        ``overdue_amount = 0 -> 1`` mutant on line 764, and the
        ``and -> or`` mutant on line 770 (the mutated ``due_status`` is the
        enum class object rather than the ``"overdue"`` string).
        """
        tenant_id = uuid4()
        ctx = _build_ctx(tenant_id=tenant_id)
        today = clock().today()
        installment_id = uuid4()
        session, invoice_id = _projection_session(
            tenant_id,
            total=1_000_000,
            allocated=100_000,
            rows=[
                _proj_row(
                    tenant_id,
                    installment_id=installment_id,
                    due_date=today - timedelta(days=1),
                    due_amount=1_000_000,
                    credited=200_000,
                    allocated=100_000,
                )
            ],
        )
        proj = await compute_invoice_projection(session, ctx, invoice_id)
        assert proj["overdue_amount"] == 700_000
        (inst,) = proj["installments"]
        assert inst["installment_id"] == str(installment_id)
        assert inst["due_amount"] == 1_000_000
        assert inst["credited"] == 200_000
        assert inst["allocated"] == 100_000
        assert inst["remainder"] == 700_000
        assert inst["due_status"] == "overdue"
        assert isinstance(inst["due_status"], str)

    @pytest.mark.parametrize(
        ("due_offset_days", "due_amount", "allocated", "expected_status", "expected_overdue"),
        [
            pytest.param(0, 2, 1, "not_due", 0, id="due_today_remainder_one"),
            pytest.param(-1, 2, 1, "overdue", 1, id="overdue_remainder_one"),
            pytest.param(-1, 2, 2, "not_due", 0, id="overdue_remainder_zero"),
            pytest.param(1, 5, 0, "not_due", 0, id="future_due_remainder_positive"),
        ],
    )
    async def test_due_status_boundary_table(
        self,
        due_offset_days: int,
        due_amount: int,
        allocated: int,
        expected_status: str,
        expected_overdue: int,
    ) -> None:
        """``due_status`` is ``overdue`` iff *past due* **and** *remainder > 0*.

        The four cells pin, one operator each, every mutant on line 771:
        ``< -> <=`` (due today), ``> 0 -> > 1`` (remainder exactly 1),
        ``> -> >=`` (remainder exactly 0), ``and -> or`` (future due date
        with a live remainder) — plus ``== -> !=`` on line 774, which would
        accumulate the not-due rows into ``overdue_amount``.
        """
        tenant_id = uuid4()
        ctx = _build_ctx(tenant_id=tenant_id)
        today = clock().today()
        session, invoice_id = _projection_session(
            tenant_id,
            total=1_000_000,
            allocated=allocated,
            rows=[
                _proj_row(
                    tenant_id,
                    due_date=today + timedelta(days=due_offset_days),
                    due_amount=due_amount,
                    allocated=allocated,
                )
            ],
        )
        proj = await compute_invoice_projection(session, ctx, invoice_id)
        assert proj["installments"][0]["due_status"] == expected_status
        assert proj["overdue_amount"] == expected_overdue

    async def test_settled_installment_never_counts_as_overdue(self) -> None:
        """A fully-allocated past-due installment contributes zero overdue.

        Complements the boundary table: an allocation larger than the due
        amount drives the remainder negative, which ``remainder > 0`` must
        reject.
        """
        tenant_id = uuid4()
        ctx = _build_ctx(tenant_id=tenant_id)
        today = clock().today()
        session, invoice_id = _projection_session(
            tenant_id,
            total=1_000_000,
            allocated=1_000_000,
            rows=[
                _proj_row(
                    tenant_id,
                    due_date=today - timedelta(days=1),
                    due_amount=1_000_000,
                    allocated=1_000_000,
                )
            ],
        )
        proj = await compute_invoice_projection(session, ctx, invoice_id)
        assert proj["installments"][0]["remainder"] == 0
        assert proj["overdue_amount"] == 0
        assert proj["payment_status"] == "paid"


class TestComputeInvoiceProjectionPaymentStatus:
    """The ``payment_status`` dispatch — V14 head-line state."""

    @pytest.mark.parametrize(
        ("total", "allocated", "overdue", "expected"),
        [
            pytest.param(0, 0, 0, "settled_zero", id="zero_invoice"),
            pytest.param(0, 5_000, 0, "settled_zero", id="zero_invoice_with_use"),
            pytest.param(1_000_000, 0, 0, "settled_zero", id="nothing_allocated_or_overdue"),
            pytest.param(1_000_000, 1_000_000, 0, "paid", id="fully_allocated"),
            pytest.param(1_000_000, 1, 0, "partial", id="one_rupiah_allocated"),
            pytest.param(1_000_000, 0, 100_000, "unpaid", id="overdue_only"),
            pytest.param(1_000_000, 100_000, 100_000, "partial", id="partial_and_overdue"),
            pytest.param(1_000_000, 0, 0, "settled_zero", id="zero_sum"),
        ],
    )
    async def test_payment_status_table(
        self, total: int, allocated: int, overdue: int, expected: str
    ) -> None:
        """``settled_zero`` / ``paid`` / ``partial`` / ``unpaid`` dispatch.

        Between them the cells pin every mutant on lines 787–791: the two
        ``== 0 -> == 1`` mutants and the ``or -> and`` mutant on 787 (via
        ``zero_invoice_with_use`` and ``zero_sum``), both ``== -> !=``
        mutants (via ``fully_allocated`` and ``overdue_only``), the
        ``+ -> -`` mutant (via ``partial_and_overdue``, where allocated ==
        overdue), ``>= -> >`` on 789 (``fully_allocated``) and both
        ``> 0 -> > 1`` / ``> -> >=`` mutants on 791 (via
        ``one_rupiah_allocated`` and ``overdue_only``).
        """
        tenant_id = uuid4()
        ctx = _build_ctx(tenant_id=tenant_id)
        today = clock().today()
        rows = (
            [
                _proj_row(
                    tenant_id,
                    due_date=today - timedelta(days=1),
                    due_amount=overdue,
                    allocated=0,
                )
            ]
            if overdue
            else []
        )
        session, invoice_id = _projection_session(
            tenant_id, total=total, allocated=allocated, rows=rows
        )
        proj = await compute_invoice_projection(session, ctx, invoice_id)
        assert proj["payment_status"] == expected
        assert proj["total_amount"] == total
        assert proj["active_allocation_total"] == allocated

    async def test_missing_invoice_is_rejected(self) -> None:
        """An unknown invoice is a validation error, not an empty projection."""
        session = _FakeSession([])
        ctx = _build_ctx()
        with pytest.raises(ValidationError, match="Invoice not found"):
            await compute_invoice_projection(session, ctx, uuid4())


# ===========================================================================
# finance/infrastructure/repositories — sequence + aggregate reads
# ===========================================================================


class TestRepositorySequenceNumbers:
    """``next_invoice_no`` / ``next_receipt_no`` — monotonic per tenant."""

    @pytest.mark.parametrize(
        ("max_n", "expected"),
        [
            pytest.param(5, "INV-000006", id="continues_from_max"),
            pytest.param(0, "INV-000001", id="first_invoice"),
            pytest.param(None, "INV-000001", id="null_max_falls_back_to_one"),
        ],
    )
    async def test_next_invoice_no(self, max_n: int | None, expected: str) -> None:
        """``(MAX(...) or 0) + 1`` — pins all four mutants on line 191.

        ``or -> and`` and ``+ -> -`` both corrupt the continuation from 5,
        ``or 0 -> or 1`` inflates it to 7, and ``+ 1 -> + 0`` stalls at 5.
        """
        session = _FakeSession(_seq(max_n))
        repo = InvoiceRepository(session, uuid4())
        assert await repo.next_invoice_no() == expected

    @pytest.mark.parametrize(
        ("max_n", "expected"),
        [
            pytest.param(5, "RCP-000006", id="continues_from_max"),
            pytest.param(0, "RCP-000001", id="first_receipt"),
            pytest.param(None, "RCP-000001", id="null_max_falls_back_to_one"),
        ],
    )
    async def test_next_receipt_no(self, max_n: int | None, expected: str) -> None:
        """The receipt sequence pins all four mutants on line 589."""
        session = _FakeSession(_seq(max_n))
        repo = ReceiptRepository(session, uuid4())
        assert await repo.next_receipt_no() == expected


class TestRepositoryAllocationSums:
    """``sum_active_allocations`` on both repositories — V15 capacity read."""

    @pytest.mark.parametrize(
        ("total", "expected"),
        [
            pytest.param(5_000, 5_000, id="active_allocations_sum"),
            pytest.param(0, 0, id="zero_sum"),
            pytest.param(None, 0, id="null_sum_falls_back_to_zero"),
        ],
    )
    @pytest.mark.parametrize(
        "repo_factory",
        [
            pytest.param(
                lambda session, tenant_id: InvoiceRepository(session, tenant_id),
                id="invoice_repo",
            ),
            pytest.param(
                lambda session, tenant_id: VerifiedPaymentRepository(session, tenant_id),
                id="verified_payment_repo",
            ),
        ],
    )
    async def test_sum_active_allocations(
        self,
        repo_factory: Any,
        total: int | None,
        expected: int,
    ) -> None:
        """``int(row.total or 0)`` — pins the ``or -> and`` / ``or 0 -> or 1``.

        The live row pins ``or -> and`` (which would fold 5000 to 0); the
        NULL row pins ``or 0 -> or 1`` (which would report 1 unit of
        capacity consumed where there is none — the V15 capacity read).
        Covers line 209 (invoice) and line 450 (verified payment).
        """
        tenant_id = uuid4()
        session = _FakeSession(_agg(total))
        repo = repo_factory(session, tenant_id)
        argument = uuid4()
        assert await repo.sum_active_allocations(argument) == expected
        # The capacity read is tenant-scoped and ignores reversed rows — a
        # reversed allocation must not keep consuming V15 capacity.
        sql, params = session.statements[0]
        assert "reversed_at IS NULL" in sql
        assert params["tid"] == str(tenant_id)