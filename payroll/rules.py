"""Versioned statutory rules for Myanmar payroll (PIT + SSB).

Design principle
----------------
**Tax rules are DATA, not code.**  Myanmar's Union Tax Law is re-enacted
every year and rates/thresholds move.  Every parameter below is therefore
carried in a :class:`StatutoryRules` record with ``effective_from`` /
``effective_to`` dating, resolved by :func:`rules_for`.

Adding a new law year means appending a record -- never editing code -- and
historical payroll runs stay reproducible because they resolve against the
rules in force on the payroll period date.

Provenance
----------
* PIT reliefs: Income Tax Law s.6(g)(2) -- 20% basic relief (capped at
  MMK 10,000,000/yr), spouse 1,000,000, child 500,000, parent 1,000,000.
* PIT bands: Union Tax Law 2025-2026 bracket table (0% -> 25%).
* SSB: Social Security Law 2012 -- 3% employer / 2% employee, wage base
  capped at MMK 300,000/month.

.. warning::
   Verify the band table against the official Union Tax Law / IRD before
   production use.  Some secondary sources still publish the pre-Oct-2021
   structure.  Because everything is a config record, a correction is a
   one-line data edit.
"""

from __future__ import annotations

import datetime as _dt
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Optional, Sequence

from .money import D

__all__ = [
    "TaxBand",
    "ReliefRule",
    "ExemptionRule",
    "SsbRule",
    "StatutoryRules",
    "MYANMAR_RULES",
    "rules_for",
    "RuleNotFound",
    "MMK",
]

MMK = D(1)

_FOREVER = _dt.date(9999, 12, 31)


class RuleNotFound(LookupError):
    """No statutory rule set is in force for the requested date."""


@dataclass(frozen=True)
class TaxBand:
    """One progressive income-tax band.

    ``lower`` is the exclusive band floor and ``upper`` the inclusive ceiling
    (``None`` for the open-ended top band).  ``rate`` is a fraction.
    """

    lower: Decimal
    upper: Optional[Decimal]
    rate: Decimal

    def __post_init__(self) -> None:
        if self.rate < 0:
            raise ValueError("rate must be >= 0")
        if self.upper is not None and self.upper <= self.lower:
            raise ValueError("upper must be > lower")

    def width(self, taxable: Decimal) -> Decimal:
        """Portion of ``taxable`` falling inside this band (>= 0)."""
        if taxable <= self.lower:
            return D(0)
        ceiling = self.upper if self.upper is not None else taxable
        return min(taxable, ceiling) - self.lower


@dataclass(frozen=True)
class ReliefRule:
    """Statutory deductions applied before the progressive bands.

    ``basic_pct`` is the basic personal relief as a fraction of income.
    ``basic_cap`` is its annual ceiling (Income Tax Law s.6(g)(2)(a)).
    """

    basic_pct: Decimal = D("0.20")
    basic_cap: Decimal = D("10000000")
    spouse: Decimal = D("1000000")
    child: Decimal = D("500000")
    parent: Decimal = D("1000000")
    # Allowances for residents only; non-residents get no reliefs at all.
    residents_only: bool = True


@dataclass(frozen=True)
class ExemptionRule:
    """The 'no PIT below this amount' threshold.

    ``basis`` controls what the threshold is measured against, because
    published secondary sources are inconsistent here and it materially
    changes the answer:

    * ``"annual_gross"``   -- threshold tested against total annual salary
    * ``"taxable_income"`` -- threshold tested against income after reliefs
    * ``"none"``           -- disable; rely on the 0% band alone
    """

    threshold: Decimal = D("4800000")
    basis: str = "annual_gross"

    def __post_init__(self) -> None:
        allowed = {"annual_gross", "taxable_income", "none"}
        if self.basis not in allowed:
            raise ValueError(f"basis must be one of {sorted(allowed)}")


@dataclass(frozen=True)
class SsbRule:
    """Social Security Board contribution parameters."""

    employee_rate: Decimal = D("0.02")
    employer_rate: Decimal = D("0.03")
    wage_ceiling: Decimal = D("300000")
    # Employers below this headcount may register voluntarily.
    mandatory_headcount: int = 5

    @property
    def total_rate(self) -> Decimal:
        return self.employee_rate + self.employer_rate


@dataclass(frozen=True)
class StatutoryRules:
    """Everything the engine needs to know for one effective period."""

    key: str
    effective_from: _dt.date
    effective_to: _dt.date = _FOREVER
    tax_bands: Sequence[TaxBand] = field(default_factory=tuple)
    relief: ReliefRule = field(default_factory=ReliefRule)
    exemption: ExemptionRule = field(default_factory=ExemptionRule)
    ssb: SsbRule = field(default_factory=SsbRule)
    source: str = ""

    def in_force_on(self, day: _dt.date) -> bool:
        return self.effective_from <= day <= self.effective_to

    def __post_init__(self) -> None:
        if self.effective_to < self.effective_from:
            raise ValueError("effective_to must be >= effective_from")
        if not self.tax_bands:
            raise ValueError("tax_bands must not be empty")
        # Bands must be contiguous and ascending.
        prev_upper: Optional[Decimal] = D(0)
        for band in self.tax_bands:
            if prev_upper is not None and band.lower != prev_upper:
                raise ValueError(
                    f"tax_bands must be contiguous: gap/overlap at {band.lower}"
                )
            prev_upper = band.upper
        if prev_upper is not None:
            raise ValueError("last tax band must be open-ended (upper=None)")


# ---------------------------------------------------------------------------
# Myanmar rule sets
# ---------------------------------------------------------------------------

_MYANMAR_BANDS = (
    TaxBand(D(0), D("2000000"), D("0.00")),
    TaxBand(D("2000000"), D("10000000"), D("0.05")),
    TaxBand(D("10000000"), D("30000000"), D("0.10")),
    TaxBand(D("30000000"), D("50000000"), D("0.15")),
    TaxBand(D("50000000"), D("70000000"), D("0.20")),
    TaxBand(D("70000000"), None, D("0.25")),
)

MYANMAR_RULES: tuple[StatutoryRules, ...] = (
    StatutoryRules(
        key="mm-utl-2025-2026",
        effective_from=_dt.date(2025, 4, 1),
        effective_to=_dt.date(2026, 3, 31),
        tax_bands=_MYANMAR_BANDS,
        relief=ReliefRule(),
        exemption=ExemptionRule(),
        ssb=SsbRule(),
        source=(
            "Union Tax Law 2025-2026; Income Tax Law s.6(g)(2); "
            "Social Security Law 2012"
        ),
    ),
    # Assume the same parameters carry into the next tax year until a new
    # Union Tax Law record is published. Replace/extend this record when the
    # official figures are confirmed.
    StatutoryRules(
        key="mm-utl-2026-2027-provisional",
        effective_from=_dt.date(2026, 4, 1),
        effective_to=_dt.date(2027, 3, 31),
        tax_bands=_MYANMAR_BANDS,
        relief=ReliefRule(),
        exemption=ExemptionRule(),
        ssb=SsbRule(),
        source="Provisional carry-forward of 2025-2026 parameters -- CONFIRM",
    ),
)


def rules_for(day: _dt.date) -> StatutoryRules:
    """Return the statutory rule set in force on ``day``.

    Raises :class:`RuleNotFound` when no record covers the date.
    """
    if not isinstance(day, _dt.date):
        raise TypeError(f"expected datetime.date, got {type(day).__name__}")
    for record in MYANMAR_RULES:
        if record.in_force_on(day):
            return record
    raise RuleNotFound(f"no statutory rules in force on {day.isoformat()}")
