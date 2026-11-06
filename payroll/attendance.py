"""Attendance summary -> payroll components.

Attendance affects gross pay in two ways:

* ``unpaid_leave`` / ``absent`` days reduce basic salary by daily rate
* ``ot_hours`` add an overtime earning at the configured multiplier

Everything else (``present``, ``leave``) is already covered by the monthly
salary and must not be double-counted.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Sequence

from .money import D, ZERO, round_mmk
from .payslip import PayComponent

__all__ = ["AttendanceSummary", "summarise_attendance", "components_from_attendance"]

# Paid days assumed in a Myanmar payroll month for daily-rate proration.
DEFAULT_PAID_DAYS = 22
DEFAULT_OT_MULTIPLIER = D("1.5")


@dataclass(frozen=True)
class AttendanceSummary:
    """One employee's attendance for one payroll period."""

    employee_id: int
    period: str
    days_present: int = 0
    half_days: int = 0
    days_leave: int = 0
    unpaid_days: int = 0
    absent_days: int = 0
    ot_hours: Decimal = ZERO
    records: tuple[tuple[str, str], ...] = field(default_factory=tuple)

    @property
    def days_worked(self) -> int:
        return self.days_present + (self.half_days // 2)

    @property
    def unpaid_total(self) -> Decimal:
        return D(self.unpaid_days) + D(self.absent_days) + D(self.half_days % 2) * D("0.5")

    @property
    def is_empty(self) -> bool:
        return (
            self.days_present == 0
            and self.half_days == 0
            and self.days_leave == 0
            and self.unpaid_days == 0
            and self.absent_days == 0
            and self.ot_hours == 0
        )


def summarise_attendance(
    employee_id: int,
    period: str,
    rows: Sequence[dict],
) -> AttendanceSummary:
    """Fold raw ``attendance`` table rows into a summary."""
    present = half = leave = unpaid = absent = 0
    ot = ZERO
    recs: list[tuple[str, str]] = []
    for r in rows:
        if int(r.get("employee_id", employee_id)) != employee_id:
            continue
        status = r.get("status", "present")
        recs.append((str(r.get("work_date", "")), status))
        if status == "present":
            present += 1
        elif status == "half_day":
            half += 1
        elif status == "leave":
            leave += 1
        elif status == "unpaid_leave":
            unpaid += 1
        elif status == "absent":
            absent += 1
        ot += D(str(r.get("ot_hours", 0) or 0))
    return AttendanceSummary(
        employee_id=employee_id,
        period=period,
        days_present=present,
        half_days=half,
        days_leave=leave,
        unpaid_days=unpaid,
        absent_days=absent,
        ot_hours=ot,
        records=tuple(recs),
    )


def components_from_attendance(
    summary: AttendanceSummary,
    basic_salary,
    *,
    paid_days: int = DEFAULT_PAID_DAYS,
    ot_multiplier=DEFAULT_OT_MULTIPLIER,
    hourly_rate=None,
) -> list[PayComponent]:
    """Derive pay components from attendance.

    Returns a ``PAY_DEDUCT`` deduction for unpaid days and a ``OT`` earning
    for overtime hours. An empty attendance record yields no components, so
    salary is paid in full -- the correct default when attendance has not
    been captured yet.
    """
    if summary.is_empty:
        return []

    basic = D(basic_salary)
    out: list[PayComponent] = []

    if summary.unpaid_total > 0:
        daily = basic / D(paid_days)
        unpaid_amount = D(round_mmk(daily * summary.unpaid_total))
        if unpaid_amount > 0:
            out.append(
                PayComponent(
                    code="UNPAID",
                    name=f"Unpaid leave / absent ({summary.unpaid_total} day(s))",
                    amount=unpaid_amount,
                    kind="deduction",
                    taxable=True,
                    ssb_eligible=True,
                    recurring=False,
                )
            )

    if summary.ot_hours > 0:
        rate = D(hourly_rate) if hourly_rate is not None else basic / D(paid_days * 8)
        ot_amount = D(round_mmk(rate * D(ot_multiplier) * summary.ot_hours))
        if ot_amount > 0:
            out.append(
                PayComponent(
                    code="OT",
                    name=f"Overtime ({summary.ot_hours} h)",
                    amount=ot_amount,
                    kind="earning",
                    taxable=True,
                    ssb_eligible=True,
                    recurring=False,
                )
            )
    return out
