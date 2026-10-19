"""Money value object.

Per the data dictionary (Money/Currency conventions) and ADR0001 §5:

- Money is **integer in the smallest unit + currency**.
- Pilot uses IDR with scale 0 (no subunit); we still use ``Decimal`` for the math
  (one Decimal, scale 0, ``ROUND_HALF_EVEN``), then convert to int at the boundary.
- No floating point anywhere — `Decimal` only.
- Negative amounts are forbidden at the value-object level except where ``FinancialAdjustment``
  explicitly permits (reversal sign is encoded in ``adjustment_kind``, not in sign).

Storage mapping: ``NUMERIC(18, 0)`` in Postgres + CHECK (amount >= 0) for non-adjustment rows.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_HALF_EVEN, Decimal
from enum import StrEnum

from ksma.shared.errors import CurrencyMismatchError, NegativeBalanceError


class Currency(StrEnum):
    IDR = "IDR"  # pilot only; Q01 / Q11 decisions may extend this


# All currencies in pilot share the same scale (0 — no subunit). This is per the
# data dictionary "Money: integer in smallest unit + Currency. Pilot proposes
# IDR with scale 0".
_SCALE = 0
_QUANTUM = Decimal(1).scaleb(-_SCALE)  # 1 for scale 0


@dataclass(frozen=True, slots=True)
class Money:
    """Immutable money value.

    ``amount`` is the integer count in the currency's smallest unit.
    For IDR (no subunit), ``amount`` is the rupiah number itself.

    Invariants:
    - ``amount >= 0`` (raise ``NegativeBalanceError`` otherwise)
    - ``amount`` fits in 18 decimal digits (Postgres ``NUMERIC(18, 0)``)
    """

    amount: int
    currency: Currency = Currency.IDR

    def __post_init__(self) -> None:
        if self.amount < 0:
            raise NegativeBalanceError(
                f"Money cannot be negative: {self.amount} {self.currency}",
                details={"amount": self.amount, "currency": str(self.currency)},
            )
        if self.amount >= 10**18:
            raise ValueError(
                f"Money amount {self.amount} exceeds NUMERIC(18,0) precision"
            )

    @classmethod
    def from_decimal(cls, value: Decimal | int | str, currency: Currency = Currency.IDR) -> "Money":
        if isinstance(value, Decimal):
            scaled = value.scaleb(_SCALE).quantize(_QUANTUM, rounding=ROUND_HALF_EVEN)
        else:
            scaled = Decimal(str(value))
        return cls(amount=int(scaled), currency=currency)

    def to_decimal(self) -> Decimal:
        return Decimal(self.amount).scaleb(-_SCALE)

    def __add__(self, other: "Money") -> "Money":
        _check_same_currency(self, other)
        return Money(self.amount + other.amount, self.currency)

    def __sub__(self, other: "Money") -> "Money":
        _check_same_currency(self, other)
        result = self.amount - other.amount
        # Allow temporary negative for projection math; raise at the domain layer
        # when an invariant requires it (e.g., available funds must be >= 0).
        if result < 0:
            raise NegativeBalanceError(
                f"Subtraction yields negative balance: {self.amount} - {other.amount}",
                details={"result": result},
            )
        return Money(result, self.currency)

    def __mul__(self, factor: int) -> "Money":
        return Money(self.amount * factor, self.currency)

    def is_zero(self) -> bool:
        return self.amount == 0

    def __str__(self) -> str:
        return f"{self.amount} {self.currency}"


def _check_same_currency(a: Money, b: Money) -> None:
    if a.currency is not b.currency:
        raise CurrencyMismatchError(
            f"Currency mismatch: {a.currency} vs {b.currency}",
            details={"left": str(a), "right": str(b)},
        )


ZERO_IDR = Money(amount=0, currency=Currency.IDR)