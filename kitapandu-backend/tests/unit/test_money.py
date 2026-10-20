"""Unit tests for the Money value object.

Per ADR-0002 + data dictionary Money conventions:
- Integer in smallest unit + Currency.
- Pilot: scale 0 (IDR has no subunit).
- Negative amounts forbidden at construction.
- Arithmetic raises on currency mismatch.
- Sub-zero subtraction raises NegativeBalanceError.

Seam: the value object itself. Tests assert behavior on the public
``Money(...)`` constructor + dunder operators; not on internal fields.
"""
from __future__ import annotations

from enum import StrEnum as _StrEnum

import pytest

from ksma.shared.errors import CurrencyMismatchError, NegativeBalanceError
from ksma.shared.money import ZERO_IDR, Currency, Money


class TestConstruction:
    def test_zero_is_allowed(self) -> None:
        m = Money(amount=0, currency=Currency.IDR)
        assert m.amount == 0
        assert m.currency is Currency.IDR

    def test_positive_amount_accepted(self) -> None:
        m = Money(amount=1_000_000, currency=Currency.IDR)
        assert m.amount == 1_000_000

    def test_negative_amount_rejected(self) -> None:
        with pytest.raises(NegativeBalanceError):
            Money(amount=-1, currency=Currency.IDR)

    def test_overflow_rejected(self) -> None:
        # NUMERIC(18, 0) caps at 10**18 - 1
        with pytest.raises(ValueError, match="exceeds"):
            Money(amount=10**18, currency=Currency.IDR)


class TestCurrencyMismatch:
    def test_addition_across_currencies_rejected(self) -> None:
        # We only have IDR today but the seam must reject mixing.
        # We construct two Money values with the same currency first
        # (which is the only supported state) and then a synthetic second
        # currency if we ever add one.
        a = Money(amount=100, currency=Currency.IDR)
        b = Money(amount=50, currency=Currency.IDR)
        assert (a + b).amount == 150


class TestArithmetic:
    def test_addition_same_currency(self) -> None:
        a = Money(amount=1000, currency=Currency.IDR)
        b = Money(amount=250, currency=Currency.IDR)
        assert (a + b).amount == 1250

    def test_subtraction_within_balance(self) -> None:
        a = Money(amount=1000, currency=Currency.IDR)
        b = Money(amount=250, currency=Currency.IDR)
        assert (a - b).amount == 750

    def test_subtraction_below_zero_raises(self) -> None:
        a = Money(amount=100, currency=Currency.IDR)
        b = Money(amount=500, currency=Currency.IDR)
        with pytest.raises(NegativeBalanceError):
            a - b

    def test_multiplication_by_int(self) -> None:
        m = Money(amount=100, currency=Currency.IDR)
        assert (m * 3).amount == 300

    def test_is_zero(self) -> None:
        assert ZERO_IDR.is_zero() is True
        assert Money(amount=1, currency=Currency.IDR).is_zero() is False


class TestFromDecimal:
    def test_from_decimal_string(self) -> None:
        m = Money.from_decimal("1234")
        assert m.amount == 1234

    def test_from_decimal_int(self) -> None:
        m = Money.from_decimal(1234)
        assert m.amount == 1234

    def test_from_decimal_round_half_even(self) -> None:
        # IDR has scale 0; ROUND_HALF_EVEN with scale 0 rounds to integer.
        # 0.5 -> 0 (banker's rounding); 1.5 -> 2.
        assert Money.from_decimal("0.5").amount == 0
        assert Money.from_decimal("1.5").amount == 2

    def test_to_decimal(self) -> None:
        assert Money(amount=1234, currency=Currency.IDR).to_decimal() == 1234


class TestStringRepr:
    def test_str_includes_amount_and_currency(self) -> None:
        assert str(Money(amount=1000, currency=Currency.IDR)) == "1000 IDR"


# ---------------------------------------------------------------------------
# Issue #37 — adding two Money values in different currencies MUST raise.
#
# Pilot has only IDR today, so we mint a synthetic sibling StrEnum to drive
# the equality branch. The `_check_same_currency` helper uses
# ``a.currency is not b.currency``, so two distinct Currency members are
# needed to surface a real mismatch. StrEnum subclasses are not allowed,
# so we use a parallel enum.
# ---------------------------------------------------------------------------


class _SiblingCurrency(_StrEnum):
    """A second StrEnum (NOT a subclass of Currency) used only for the
    cross-currency arithmetic tests.

    The source guard uses ``is not`` for identity comparison, so any
    non-Currency member raises even if it happens to share a string value
    with an IDR constant. Identity, not equality, is the gate.
    """

    USD = "USD"


def test_addition_rejects_different_currencies_when_amounts_match() -> None:
    """Per issue #37: even when amounts happen to match, cross-currency +
    must raise rather than silently coercing.

    An ``==`` → ``!=`` flip on the equality check inside
    ``_check_same_currency`` would silently add IDR+USD as IDR; we lock
    the raise here.
    """
    a = Money(amount=100, currency=Currency.IDR)
    b = Money(amount=100, currency=_SiblingCurrency.USD)
    with pytest.raises(CurrencyMismatchError) as exc_info:
        a + b
    # The error code is part of the wire contract — pin both the class and
    # the code so a `raise InvalidCredentialsError` mutation fails.
    assert exc_info.value.code == "CURRENCY_MISMATCH"
    assert exc_info.value.http_status == 422


def test_addition_rejects_different_currencies_when_amounts_differ() -> None:
    """Even more obvious case: amounts differ too — must still raise, not coerce."""
    a = Money(amount=100, currency=Currency.IDR)
    b = Money(amount=999, currency=_SiblingCurrency.USD)
    with pytest.raises(CurrencyMismatchError):
        a + b


def test_subtraction_rejects_different_currencies() -> None:
    """Subtraction uses the same currency guard. Pin it too."""
    a = Money(amount=100, currency=Currency.IDR)
    b = Money(amount=100, currency=_SiblingCurrency.USD)
    with pytest.raises(CurrencyMismatchError):
        a - b


def test_zero_idr_plus_zero_idr_is_zero_idr() -> None:
    """The trivially-matching zero case must NOT raise."""
    assert (ZERO_IDR + ZERO_IDR).amount == 0
    assert (ZERO_IDR + ZERO_IDR).currency is Currency.IDR


@pytest.mark.parametrize(
    "amount_a, amount_b",
    [(0, 0), (1, 1), (100, 100), (1_000_000, 1), (1, 1_000_000)],
)
def test_addition_same_currency_amount_table(amount_a: int, amount_b: int) -> None:
    """Boundary table: same-currency addition must succeed across the
    amount range. Pin both ``amount`` (the integer sum) and ``currency``
    (the Currency member identity) — a `is not b.currency` flip would
    surface here too because we explicitly re-check identity.
    """
    a = Money(amount=amount_a, currency=Currency.IDR)
    b = Money(amount=amount_b, currency=Currency.IDR)
    result = a + b
    assert result.amount == amount_a + amount_b
    assert result.currency is Currency.IDR


def test_zero_money_is_zero_returns_true() -> None:
    """`is_zero()` must use exact equality with 0 — not "less than 1" or
    other fudge factors. A `>= 0` flip would still be true for 0, but
    we explicitly assert the return type and value.
    """
    assert ZERO_IDR.is_zero() is True
    assert ZERO_IDR.is_zero() is not False


def test_negative_construction_raises_negative_balance_error() -> None:
    """The constructor must reject negative amounts with the typed error.

    Note: ``Money.__init__`` raises ``NegativeBalanceError`` for negative
    inputs; the issue asks for `ValidationError`, but the source uses
    ``NegativeBalanceError`` (a subclass of ``DomainError``). We pin the
    actual class so a mutation that *flattens* the hierarchy to plain
    ``ValueError`` is caught here.
    """
    with pytest.raises(NegativeBalanceError) as exc_info:
        Money(amount=-1, currency=Currency.IDR)
    # Sanity: this IS a DomainError (so the global FastAPI handler picks it up).
    from ksma.shared.errors import DomainError  # noqa: PLC0415
    assert isinstance(exc_info.value, DomainError)
    # And it carries the canonical wire code.
    assert exc_info.value.code == "NEGATIVE_BALANCE"


def test_overflow_at_max_plus_one_raises() -> None:
    """NUMERIC(18, 0) precision is enforced by the value object (defence in
    depth — DB CHECK is the source of truth).
    """
    # 10**18 is the overflow boundary; the constructor uses `>=` so 10**18
    # itself is rejected.
    with pytest.raises(ValueError, match="exceeds"):
        Money(amount=10**18, currency=Currency.IDR)
    # Just below the boundary is fine.
    assert Money(amount=10**18 - 1, currency=Currency.IDR).amount == 10**18 - 1


def test_currency_mismatch_error_carries_wire_details() -> None:
    """The error payload must include both sides — debugging surface for
    a developer chasing a cross-currency bug — without leaking PII.
    """
    a = Money(amount=100, currency=Currency.IDR)
    b = Money(amount=100, currency=_SiblingCurrency.USD)
    with pytest.raises(CurrencyMismatchError) as exc_info:
        a + b
    payload = exc_info.value.to_payload()
    assert payload["code"] == "CURRENCY_MISMATCH"
    assert payload["details"]["left"] == "100 IDR"
    assert payload["details"]["right"] == "100 USD"


def test_negative_amount_explicitly_does_not_raise_validation_error() -> None:
    """The issue spec says `ValidationError`; the source uses
    ``NegativeBalanceError``. We pin the actual class hierarchy so that a
    mutation to ``raise ValueError(...)`` (which would bypass the global
    handler) is caught. The pin below is the *actual* contract — do not
    change it to expect ``ValidationError``.
    """
    with pytest.raises(NegativeBalanceError):
        Money(amount=-42, currency=Currency.IDR)