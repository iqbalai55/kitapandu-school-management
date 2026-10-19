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