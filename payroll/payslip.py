"""Payslip composition -- ties the PIT and SSB engines together.

One payslip is produced for one employee for one payroll period:

    gross earnings
      -> annualised for PIT
      -> PIT (monthly withholding)
      -> SSB (employee share is deducted; employer share is a company cost)
      -> other deductions
      -> net pay

The employer's SSB share never reduces the employee's net pay; it is carried
on the payslip as a cost-to-company figure only.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Iterable, Literal, Sequence

from .money import D, ZERO, round_mmk
from .pit import PitBreakdown, TaxProfile, calculate_pit
from .rules import StatutoryRules, rules_for
from .ssb import SsbBreakdown, calculate_ssb

__all__ = [
    "ComponentKind",
    "PayComponent",
    "Payslip",
    "EmployeePayInput",
    "calculate_payslip",
    "run_payroll",
]

ComponentKind = Literal["earning", "deduction", "reimbursement"]


@dataclass(frozen=True)
class PayComponent:
    """One line on a payslip.

    ``taxable``     -- included in PIT income (basic salary, allowance, bonus)
    ``ssb_eligible``-- included in the SSB contribution wage base
    ``recurring``   -- recurs every month (used when annualising for PIT)

    A reimbursement (e.g. actual travel cost) is neither taxable nor
    SSB-eligible and does not reduce net pay because it is paid back out.
    """

    code: str
    name: str
    amount: Decimal
    kind: ComponentKind = "earning"
    taxable: bool = True
    ssb_eligible: bool = True
    recurring: bool = True

    def __post_init__(self) -> None:
        object.__setattr__(self, "amount", D(self.amount))
        if self.amount < 0:
            raise ValueError(f"{self.code}: amount must be >= 0")
        if self.kind == "reimbursement":
            object.__setattr__(self, "taxable", False)
            object.__setattr__(self, "ssb_eligible", False)


@dataclass(frozen=True)
class EmployeePayInput:
    """Everything needed to compute one employee's payslip."""

    employee_id: str
    name: str
    period: date
    components: Sequence[PayComponent] = ()
    tax_profile: TaxProfile = field(default_factory=TaxProfile)
    # One-off annual amounts not carried in monthly components.
    annual_bonus: Decimal = ZERO
    annual_benefits_in_kind: Decimal = ZERO
    other_deductions: Decimal = ZERO

    @property
    def recurring_taxable(self) -> Decimal:
        return sum(
            (c.amount for c in self.components if c.taxable and c.recurring),
            ZERO,
        )

    @property
    def non_recurring_taxable(self) -> Decimal:
        return sum(
            (c.amount for c in self.components if c.taxable and not c.recurring),
            ZERO,
        )

    @property
    def ssb_base_gross(self) -> Decimal:
        return sum(
            (c.amount for c in self.components if c.ssb_eligible),
            ZERO,
        )

    @property
    def gross_earnings(self) -> Decimal:
        return sum((c.amount for c in self.components if c.kind != "deduction"), ZERO)

    @property
    def component_deductions(self) -> Decimal:
        return sum(
            (c.amount for c in self.components if c.kind == "deduction"), ZERO
        )

    @property
    def reimbursements(self) -> Decimal:
        return sum(
            (c.amount for c in self.components if c.kind == "reimbursement"), ZERO
        )


@dataclass(frozen=True)
class Payslip:
    """A fully computed payslip with an audit trail."""

    employee_id: str
    name: str
    period: date
    rule_key: str
    components: tuple[PayComponent, ...]
    annual_gross: Decimal
    taxable_income: Decimal
    ssb_base: Decimal
    pit: PitBreakdown
    ssb: SsbBreakdown
    gross_earnings: Decimal
    total_deductions: Decimal
    net_pay: Decimal
    reimbursements: Decimal
    employer_ssb_cost: Decimal

    def rows(self) -> list[tuple[str, Decimal]]:
        """Human-readable (label, amount) rows for display/export."""
        out: list[tuple[str, Decimal]] = [("EARNINGS", self.gross_earnings)]
        for c in self.components:
            if c.kind == "earning":
                out.append((f"  {c.name}", c.amount))
        out += [
            ("DEDUCTIONS", self.total_deductions),
            ("  PIT (monthly withholding)", self.pit.monthly_tax),
            ("  SSB employee (2%)", self.ssb.employee_share),
        ]
        for c in self.components:
            if c.kind == "deduction":
                out.append((f"  {c.name}", c.amount))
        out += [
            ("NET PAY", self.net_pay),
            ("--- memo ---", ZERO),
            ("Employer SSB cost (3%)", self.employer_ssb_cost),
            ("Annual gross (PIT basis)", self.annual_gross),
            ("Taxable income", self.taxable_income),
        ]
        return out


def calculate_payslip(
    emp: EmployeePayInput,
    *,
    rules: StatutoryRules | None = None,
) -> Payslip:
    """Compute one payslip."""
    rules = rules or rules_for(emp.period)

    # --- PIT: annualise recurring taxable pay, add one-offs -------------
    annual_gross = (
        D(round_mmk(emp.recurring_taxable)) * 12
        + D(round_mmk(emp.non_recurring_taxable))
        + D(round_mmk(emp.annual_bonus))
        + D(round_mmk(emp.annual_benefits_in_kind))
    )
    pit = calculate_pit(annual_gross, emp.tax_profile, rules=rules)

    # --- SSB: monthly wage base ----------------------------------------
    ssb = calculate_ssb(emp.ssb_base_gross, rules=rules)

    # --- net pay -------------------------------------------------------
    gross = D(round_mmk(emp.gross_earnings))
    total_deductions = (
        pit.monthly_tax
        + ssb.employee_share
        + D(round_mmk(emp.component_deductions))
        + D(round_mmk(emp.other_deductions))
    )
    net = gross - total_deductions

    return Payslip(
        employee_id=emp.employee_id,
        name=emp.name,
        period=emp.period,
        rule_key=rules.key,
        components=tuple(emp.components),
        annual_gross=annual_gross,
        taxable_income=pit.taxable_income,
        ssb_base=ssb.contribution_base,
        pit=pit,
        ssb=ssb,
        gross_earnings=gross,
        total_deductions=D(round_mmk(total_deductions)),
        net_pay=D(round_mmk(net)),
        reimbursements=D(round_mmk(emp.reimbursements)),
        employer_ssb_cost=ssb.employer_share,
    )


@dataclass(frozen=True)
class PayrollRunResult:
    """Aggregate result for a whole payroll run."""

    period: date
    rule_key: str
    slips: tuple[Payslip, ...]

    @property
    def headcount(self) -> int:
        return len(self.slips)

    @property
    def total_gross(self) -> Decimal:
        return sum((s.gross_earnings for s in self.slips), ZERO)

    @property
    def total_pit(self) -> Decimal:
        return sum((s.pit.monthly_tax for s in self.slips), ZERO)

    @property
    def total_ssb_employee(self) -> Decimal:
        return sum((s.ssb.employee_share for s in self.slips), ZERO)

    @property
    def total_ssb_employer(self) -> Decimal:
        return sum((s.employer_ssb_cost for s in self.slips), ZERO)

    @property
    def total_net(self) -> Decimal:
        return sum((s.net_pay for s in self.slips), ZERO)

    @property
    def total_cost_to_company(self) -> Decimal:
        return self.total_gross + self.total_ssb_employer


def run_payroll(
    employees: Iterable[EmployeePayInput],
    *,
    rules: StatutoryRules | None = None,
) -> PayrollRunResult:
    """Compute payslips for a whole run.

    Raises ``ValueError`` on duplicate employee ids so a run cannot silently
    double-pay someone.
    """
    employees = list(employees)
    if not employees:
        raise ValueError("payroll run has no employees")

    seen: set[str] = set()
    for e in employees:
        if e.employee_id in seen:
            raise ValueError(f"duplicate employee_id in run: {e.employee_id}")
        seen.add(e.employee_id)

    periods = {e.period for e in employees}
    if len(periods) != 1:
        raise ValueError(f"all employees must share one period, got {sorted(periods)}")

    rules = rules or rules_for(next(iter(periods)))
    slips = tuple(calculate_payslip(e, rules=rules) for e in employees)
    return PayrollRunResult(period=next(iter(periods)), rule_key=rules.key, slips=slips)
