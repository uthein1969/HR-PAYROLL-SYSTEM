"""Service layer: database rows -> calculation engine -> stored results.

This is the seam between persistence and the pure calculation core. It never
contains tax logic of its own; it only assembles inputs and records outputs.
"""

from __future__ import annotations

import datetime as _dt
from decimal import Decimal
from typing import Optional

from .attendance import (
    DEFAULT_OT_MULTIPLIER,
    DEFAULT_PAID_DAYS,
    components_from_attendance,
    summarise_attendance,
)
from .db import Database
from .money import D, ZERO
from .payslip import (
    EmployeePayInput,
    PayComponent,
    Payslip,
    PayrollRunResult,
    calculate_payslip,
    run_payroll,
)
from .pit import TaxProfile
from .rules import StatutoryRules, rules_for

__all__ = ["PayrollService", "month_bounds", "tax_year_for"]


def month_bounds(period: str) -> tuple[str, str]:
    """Return (first_day, last_day) ISO dates for a ``YYYY-MM`` period."""
    first = _dt.date.fromisoformat(f"{period}-01")
    if first.month == 12:
        next_first = _dt.date(first.year + 1, 1, 1)
    else:
        next_first = _dt.date(first.year, first.month + 1, 1)
    last = next_first - _dt.timedelta(days=1)
    return first.isoformat(), last.isoformat()


def tax_year_for(day: _dt.date) -> str:
    """Myanmar tax year runs 1 April - 31 March.

    ``2026-06`` falls in tax year 2026-2027; ``2026-02`` in 2025-2026.
    """
    return f"{day.year}-{day.year + 1}" if day.month >= 4 else f"{day.year - 1}-{day.year}"


class PayrollService:
    """High-level operations used by the web layer."""

    def __init__(self, db: Database):
        self.db = db

    # -- inputs ----------------------------------------------------------

    def tax_profile(self, employee_id: int, tax_year: str) -> TaxProfile:
        emp = self.db.employee(employee_id)
        is_resident = (emp or {}).get("residency") != "non_resident"
        decl = self.db.tax_declaration(employee_id, tax_year) or {}
        return TaxProfile(
            is_resident=is_resident,
            spouse_claimed=bool(decl.get("spouse_claimed", 0)),
            children=int(decl.get("children_count", 0) or 0),
            parents=int(decl.get("parents_count", 0) or 0),
            insurance_premium=D(decl.get("insurance_premium", 0) or 0),
            savings=D(decl.get("savings", 0) or 0),
        )

    def base_components(self, employee_id: int) -> list[PayComponent]:
        return [
            PayComponent(
                code=r["code"],
                name=r["name"],
                amount=D(r["amount"]),
                kind=r["kind"],
                taxable=bool(r["taxable"]),
                ssb_eligible=bool(r["ssb_eligible"]),
                recurring=bool(r["recurring"]),
            )
            for r in self.db.components(employee_id)
        ]

    def build_employee_input(
        self,
        employee_id: int,
        period: str,
        *,
        rules: Optional[StatutoryRules] = None,
        include_attendance: bool = True,
    ) -> Optional[EmployeePayInput]:
        """Assemble :class:`EmployeePayInput` from stored data.

        Returns ``None`` when the employee has no salary components at all
        (e.g. an unpaid intern), so they are skipped rather than paid zero.
        """
        emp = self.db.employee(employee_id)
        if emp is None or emp.get("status") != "active":
            return None

        period_day = _dt.date.fromisoformat(f"{period}-01")
        rules = rules or rules_for(period_day)

        comps = self.base_components(employee_id)
        basic = sum(
            (c.amount for c in comps if c.code == "BASIC" and c.kind == "earning"), ZERO
        )

        if include_attendance and basic > 0:
            d_from, d_to = month_bounds(period)
            rows = self.db.attendance_between(d_from, d_to, employee_id)
            summary = summarise_attendance(employee_id, period, rows)
            comps.extend(
                components_from_attendance(
                    summary, basic, ot_multiplier=DEFAULT_OT_MULTIPLIER
                )
            )

        if not comps:
            return None

        return EmployeePayInput(
            employee_id=str(employee_id),
            name=emp["name"],
            period=period_day,
            components=tuple(comps),
            tax_profile=self.tax_profile(employee_id, tax_year_for(period_day)),
        )

    # -- payroll run -------------------------------------------------------

    def compute_run(
        self,
        period: str,
        employee_ids: Optional[list[int]] = None,
        *,
        include_attendance: bool = True,
    ) -> tuple[PayrollRunResult, int]:
        """Compute and persist a payroll run.

        Returns ``(result, run_id)``.
        """
        period_day = _dt.date.fromisoformat(f"{period}-01")
        rules = rules_for(period_day)

        if employee_ids is None:
            employee_ids = [e["id"] for e in self.db.employees(active_only=True)]

        inputs = []
        for eid in employee_ids:
            emp_input = self.build_employee_input(
                eid, period, rules=rules, include_attendance=include_attendance
            )
            if emp_input is not None:
                inputs.append(emp_input)

        if not inputs:
            raise ValueError("no payable employees for this period")

        result = run_payroll(inputs, rules=rules)

        run_id = self.db.create_run(period, rule_key=rules.key)
        for slip in result.slips:
            self.db.save_payslip(run_id, self._slip_row(slip, period))
        return result, run_id

    @staticmethod
    def _slip_row(slip: Payslip, period: str) -> dict:
        breakdown = [
            {"label": lab, "amount": str(amt)} for lab, amt in slip.pit.breakdown_rows()
        ]
        return {
            "employee_id": int(slip.employee_id),
            "employee_code": "",
            "employee_name": slip.name,
            "period": period,
            "rule_key": slip.rule_key,
            "gross_earnings": str(slip.gross_earnings),
            "total_deductions": str(slip.total_deductions),
            "net_pay": str(slip.net_pay),
            "annual_gross": str(slip.annual_gross),
            "taxable_income": str(slip.taxable_income),
            "basic_relief": str(slip.pit.basic_relief),
            "allowances_total": str(slip.pit.allowances_total),
            "pit_annual": str(slip.pit.annual_tax),
            "pit_monthly": str(slip.pit.monthly_tax),
            "ssb_base": str(slip.ssb.contribution_base),
            "ssb_employee": str(slip.ssb.employee_share),
            "ssb_employer": str(slip.ssb.employer_share),
            "reimbursements": str(slip.reimbursements),
            "breakdown_json": __import__("json").dumps(breakdown),
        }

    # -- attendance ---------------------------------------------------

    def attendance_matrix(self, period: str) -> dict:
        """Return per-employee attendance totals for a period."""
        d_from, d_to = month_bounds(period)
        rows = self.db.attendance_between(d_from, d_to)
        by_emp: dict[int, list] = {}
        for r in rows:
            by_emp.setdefault(int(r["employee_id"]), []).append(r)
        out = {}
        for emp in self.db.employees(active_only=True):
            summary = summarise_attendance(emp["id"], period, by_emp.get(emp["id"], []))
            out[emp["id"]] = summary
        return out

    def attendance_summary_rows(self, period: str) -> list[dict]:
        matrix = self.attendance_matrix(period)
        rows = []
        for emp in self.db.employees(active_only=True):
            s = matrix[emp["id"]]
            rows.append(
                {
                    "employee": emp,
                    "summary": s,
                    "total_days": s.days_present + s.half_days + s.days_leave
                    + s.unpaid_days + s.absent_days,
                }
            )
        return rows
