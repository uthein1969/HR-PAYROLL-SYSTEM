"""Social Security Board (SSB) contribution engine.

Social Security Law 2012:
* employer  3% of the contribution wage base
* employee  2% of the contribution wage base
* the wage base is capped at MMK 300,000/month -- salary above the ceiling
  is outside the SSB system entirely
* mandatory for employers with 5 or more workers
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from .money import D, ZERO, round_mmk
from .rules import SsbRule, StatutoryRules, rules_for

__all__ = ["SsbBreakdown", "calculate_ssb", "is_ssb_mandatory"]


@dataclass(frozen=True)
class SsbBreakdown:
    """SSB contribution for one employee for one month."""

    monthly_salary: Decimal
    contribution_base: Decimal
    employee_share: Decimal
    employer_share: Decimal
    ceiling_applied: bool
    rule_key: str

    @property
    def total(self) -> Decimal:
        return self.employee_share + self.employer_share

    def breakdown_rows(self) -> list[tuple[str, Decimal]]:
        return [
            ("Monthly salary", self.monthly_salary),
            ("SSB contribution base (capped)", self.contribution_base),
            ("SSB employee 2%", self.employee_share),
            ("SSB employer 3%", self.employer_share),
            ("SSB total", self.total),
        ]


def calculate_ssb(
    monthly_salary,
    *,
    rules: StatutoryRules | None = None,
    period: date | None = None,
    ssb: SsbRule | None = None,
) -> SsbBreakdown:
    """Compute the employee and employer SSB shares for one month."""
    rule_key = ""
    if ssb is None:
        if rules is None:
            rules = rules_for(period or date.today())
        ssb = rules.ssb
        rule_key = rules.key

    salary = D(monthly_salary)
    if salary < 0:
        raise ValueError("monthly_salary must be >= 0")

    base = min(salary, ssb.wage_ceiling)
    return SsbBreakdown(
        monthly_salary=salary,
        contribution_base=base,
        employee_share=D(round_mmk(base * ssb.employee_rate)),
        employer_share=D(round_mmk(base * ssb.employer_rate)),
        ceiling_applied=salary > ssb.wage_ceiling,
        rule_key=rule_key,
    )


def is_ssb_mandatory(headcount: int, ssb: SsbRule | None = None) -> bool:
    """Whether SSB registration is compulsory at this headcount."""
    ssb = ssb or SsbRule()
    return headcount >= ssb.mandatory_headcount
