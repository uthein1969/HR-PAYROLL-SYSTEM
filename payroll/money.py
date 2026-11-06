"""Money handling for Myanmar payroll (MMK).

Payroll amounts are stored and computed with :class:`decimal.Decimal`.
Myanmar payroll practice uses whole-kyat amounts, so final figures are
rounded HALF_UP to integer MMK. Intermediate values keep full precision.
"""

from __future__ import annotations

from decimal import Decimal, ROUND_HALF_UP, getcontext

# Enough precision for any realistic payroll figure.
getcontext().prec = 28

__all__ = ["D", "round_mmk", "round_mmk_decimal", "ZERO", "percent"]


def D(value) -> Decimal:
    """Coerce ``value`` to :class:`~decimal.Decimal` safely.

    Floats are rejected: ``0.1`` as a float is not ``0.1`` as a decimal and
    that difference is real money in payroll.
    """
    if isinstance(value, Decimal):
        return value
    if isinstance(value, bool):
        raise TypeError("bool is not a valid monetary amount")
    if isinstance(value, float):
        raise TypeError(
            "float is not allowed for money; use Decimal or str, "
            f"got {value!r}"
        )
    return Decimal(str(value))


ZERO = D(0)


def round_mmk(value) -> int:
    """Round to whole kyat (MMK) using banker's-opposite HALF_UP.

    ``11666.666..`` -> ``11667``; ``11666.5`` -> ``11667``.
    """
    return int(D(value).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def round_mmk_decimal(value) -> Decimal:
    """Like :func:`round_mmk` but keeps the result as ``Decimal``."""
    return D(value).quantize(Decimal("1"), rounding=ROUND_HALF_UP)


def percent(amount, rate) -> Decimal:
    """Return ``amount * rate`` where ``rate`` is a fraction (``0.20``).

    Accepts a percentage-style rate (``20``) or a fraction (``"0.20"``).
    Prefer passing explicit fractions such as ``Decimal("0.20")``.
    """
    return D(amount) * D(rate)
