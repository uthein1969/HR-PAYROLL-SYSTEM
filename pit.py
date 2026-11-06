"""Myanmar Personal Income Tax (PIT) engine.

Computation follows the statutory order:

1. ``annual_gross``  = 12 months of salary + bonuses + benefits in kind
2. ``basic_relief``  = min(annual_gross x 20%, cap)     [s.6(g)(2)(a)]
3. ``allowances``    = spouse + children + parents + insurance + savings
4. ``taxable_income`` = max(0, annual_gross - basic_relief - allowances)
5. exemption threshold check
6. ``annual_tax``    = sum over progressive bands
7. ``monthly_tax``   = annual_tax / 12   (what the employer withholds)

Non-residents take no reliefs at all and are taxed on the gross figure
through the same bands (Income Tax Law s.6 / s.24).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Sequence

from .money import D, ZERO, round_mmk
from .rules import StatutoryRules, TaxBand, rules_for

__all__ = ["TaxProfile", "BandSlice", "PitBreakdown", "calculate_pit", "PIT"]


@dataclass(frozen=True)
class TaxProfile:
    """Employee's declared reliefs for one tax year."""

    is_resident: bool = True
    spouse_claimed: bool = False
    children: int = 0
    parents: int = 0
    insurance_premium: Decimal = ZERO
    savings: Decimal = ZERO

    def __post_init__(self) -> None:
        if self.children < 0 or self.parents < 0:
            raise ValueError("children/parents must be >= 0")
        if self.insurance_premium < 0 or self.savings < 0:
            raise ValueError("insurance_premium/savings must be >= 0")


@dataclass(frozen=True)
class BandSlice:
    """How much taxable income landed in one band, and the tax on it."""

    band: TaxBand
    amount: Decimal
    tax: Decimal

    @property
    def rate(self) -> Decimal:
        return self.band.rate


@dataclass(frozen=True)
class PitBreakdown:
    """Full audit trail for one PIT calculation."""

    annual_gross: Decimal
    basic_relief: Decimal
    spouse_allowance: Decimal
    child_allowance: Decimal
    parent_allowance: Decimal
    other_relief: Decimal
    taxable_income: Decimal
    exempt: bool
    band_slices: tuple[BandSlice, ...]
    annual_tax: Decimal
    monthly_tax: Decimal
    rule_key: str

    @property
    def allowances_total(self) -> Decimal:
        return (
            self.spouse_allowance
            + self.child_allowance
            + self.parent_allowance
            + self.other_relief
        )

    @property
    def total_relief(self) -> Decimal:
        return self.basic_relief + self.allowances_total

    def breakdown_rows(self) -> list[tuple[str, Decimal]]:
        """Ordered (label, amount) rows for a payslip or tax worksheet."""
        rows: list[tuple[str, Decimal]] = [
            ("Annual gross income", self.annual_gross),
            ("Basic relief (20%, capped)", -self.basic_relief),
        ]
        if self.spouse_allowance:
            rows.append(("Spouse allowance", -self.spouse_allowance))
        if self.child_allowance:
            rows.append(("Child allowance", -self.child_allowance))
        if self.parent_allowance:
            rows.append(("Parent allowance", -self.parent_allowance))
        if self.other_relief:
            rows.append(("Insurance / savings relief", -self.other_relief))
        rows.append(("Taxable income", self.taxable_income))
        for sl in self.band_slices:
            if sl.amount == 0:
                continue
            pct = sl.rate * 100
            rows.append(
                (
                    f"  Band {sl.band.lower:,.0f}"
                    f"{'+' if sl.band.upper is None else f' - {sl.band.upper:,.0f}'}"
                    f" @ {pct:g}%",
                    sl.tax,
                )
            )
        rows.append(
            ("Annual PIT" + (" (exempt)" if self.exempt else ""), self.annual_tax)
        )
        rows.append(("Monthly PIT withheld", self.monthly_tax))
        return rows


# Public alias so callers can write ``PIT.calculate(...)``
class PIT:  # noqa: N801 - deliberate namespace alias
    """Convenience namespace mirroring :func:`calculate_pit`."""

    calculate = staticmethod(lambda *a, **kw: calculate_pit(*a, **kw))
    Profile = TaxProfile


def _band_slices(taxable: Decimal, bands: Sequence[TaxBand]) -> tuple[BandSlice, ...]:
    slices: list[BandSlice] = []
    for band in bands:
        width = band.width(taxable)
        tax = width * band.rate
        slices.append(BandSlice(band=band, amount=width, tax=tax))
    return tuple(slices)


def calculate_pit(
    annual_gross_income,
    profile: TaxProfile | None = None,
    *,
    rules: StatutoryRules | None = None,
    period: date | None = None,
) -> PitBreakdown:
    """Compute annual + monthly PIT for one employee.

    Parameters
    ----------
    annual_gross_income:
        Total salary income for the tax year, including bonuses and taxable
        benefits in kind. Pass a ``Decimal`` (or ``str``) in MMK.
    profile:
        The employee's declared reliefs. Defaults to a single filer with no
        dependants.
    rules:
        Explicit rule set. If omitted, the set in force on ``period`` is used.
    period:
        Date used to resolve the rule set when ``rules`` is not given.
        Defaults to today.
    """
    profile = profile or TaxProfile()
    if rules is None:
        rules = rules_for(period or date.today())

    annual_gross = D(annual_gross_income)
    if annual_gross < 0:
        raise ValueError("annual_gross_income must be >= 0")

    relief = rules.relief
    gets_relief = profile.is_resident or not relief.residents_only

    if gets_relief:
        basic_relief = min(annual_gross * relief.basic_pct, relief.basic_cap)
        spouse = relief.spouse if profile.spouse_claimed else ZERO
        child = relief.child * profile.children
        parent = relief.parent * profile.parents
        other = D(profile.insurance_premium) + D(profile.savings)
    else:
        # Non-resident: no personal reliefs (Income Tax Law s.24).
        basic_relief = spouse = child = parent = other = ZERO

    taxable = max(ZERO, annual_gross - basic_relief - spouse - child - parent - other)

    # Exemption threshold.
    ex = rules.exemption
    if ex.basis == "annual_gross":
        exempt = annual_gross <= ex.threshold
    elif ex.basis == "taxable_income":
        exempt = taxable <= ex.threshold
    else:  # "none"
        exempt = False

    if exempt:
        slices = tuple(
            BandSlice(band=b, amount=ZERO, tax=ZERO) for b in rules.tax_bands
        )
        annual_tax = ZERO
    else:
        slices = _band_slices(taxable, rules.tax_bands)
        annual_tax = sum((s.tax for s in slices), ZERO)

    return PitBreakdown(
        annual_gross=annual_gross,
        basic_relief=basic_relief,
        spouse_allowance=spouse,
        child_allowance=child,
        parent_allowance=parent,
        other_relief=other,
        taxable_income=taxable,
        exempt=exempt,
        band_slices=slices,
        annual_tax=annual_tax,
        monthly_tax=D(round_mmk(annual_tax / 12)),
        rule_key=rules.key,
    )
