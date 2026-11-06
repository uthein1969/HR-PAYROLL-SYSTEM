"""Demo data for the Myanmar HR Payroll System.

Creates a realistic small office: departments, salary structures, tax
declarations with dependants, and a month of attendance including unpaid
leave and overtime so the dashboard has meaningful numbers.

Run with::

    python3 seed.py                 # seeds ./payroll.db
    python3 seed.py --count 60
"""

from __future__ import annotations

import argparse
import datetime as _dt
import os
import random
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from payroll.db import Database  # noqa: E402
from payroll.money import D  # noqa: E402
from payroll.service import tax_year_for  # noqa: E402

DEPARTMENTS = [
    ("Engineering", ["Software Engineer", "Senior Engineer", "QA Engineer", "DevOps Engineer"]),
    ("Finance", ["Accountant", "Finance Officer", "Payroll Executive", "Finance Manager"]),
    ("HR & Admin", ["HR Executive", "HR Manager", "Admin Officer", "Recruiter"]),
    ("Sales", ["Sales Executive", "Sales Manager", "Account Manager", "Marketing Officer"]),
    ("Operations", ["Operations Officer", "Logistics Coordinator", "Store Keeper", "Supervisor"]),
]

FIRST_NAMES = [
    "Aung Kyaw", "Thiha", "Zaw Lin", "Kyaw Zin", "Min Thu", "Htet Aung", "Nay Lin",
    "Su Myat", "Khin Mar", "Hnin Wai", "Yamin", "Ei Phyu", "Thiri", "Nwe Oo",
    "Myo Min", "Win Htut", "Kaung Htet", "Lin Htet", "Soe Moe", "Phyo Wai",
    "Aye Chan", "Moe Yan", "Hla Hla", "Than Than", "Ko Ko", "Nyunt Win",
]
LAST_NAMES = ["Win", "Naing", "Oo", "Aung", "Soe", "Min", "Tun", "Kyaw", "Htwe", "San"]


def _mk_salary(rng: random.Random, grade: int) -> int:
    base = {1: 350_000, 2: 550_000, 3: 850_000, 4: 1_400_000, 5: 2_400_000}[grade]
    return int(round((base + rng.randint(0, base // 3)) / 5000) * 5000)


def seed_demo(db_path: str, count: int = 24, seed: int = 20261106) -> int:
    rng = random.Random(seed)
    db = Database(db_path)

    if db.headcount(active_only=False) > 0:
        print("  database already has employees; skipping reseed.")
        return 0

    period_day = _dt.date.today().replace(day=1)
    tax_year = tax_year_for(period_day)
    created = 0

    for i in range(count):
        dept, positions = DEPARTMENTS[i % len(DEPARTMENTS)]
        grade = 1 + (i % 5)
        name = f"{rng.choice(FIRST_NAMES)} {rng.choice(LAST_NAMES)}"
        code = f"EMP-{i + 1:03d}"
        join_year = rng.choice([2021, 2022, 2023, 2024, 2025, 2026])
        join_month = rng.randint(1, 12 if join_year < 2026 else 10)
        eid = db.add_employee(
            code=code,
            name=name,
            nrc=f"12/{rng.choice('ABC')}{rng.randint(100000, 999999)}",
            email=f"{code.lower()}@example.com.mm",
            phone=f"09{rng.randint(700000000, 999999999)}",
            department=dept,
            position=positions[i % len(positions)],
            join_date=f"{join_year}-{join_month:02d}-{rng.randint(1, 28):02d}",
            status="active",
            residency="resident",
            bank_account=f"{rng.randint(1000000000000000, 9999999999999999)}",
        )
        created += 1

        # salary structure
        basic = _mk_salary(rng, grade)
        db.add_component(eid, code="BASIC", name="Basic Salary", amount=basic)
        db.add_component(
            eid, code="ALLOW", name="Position Allowance",
            amount=int(round(basic * rng.choice([0.1, 0.15, 0.2]) / 1000) * 1000),
        )
        if rng.random() < 0.4:
            db.add_component(
                eid, code="PHONE", name="Phone Allowance", amount=rng.choice([30000, 50000]),
                taxable=True, ssb_eligible=True,
            )
        if rng.random() < 0.25:
            db.add_component(
                eid, code="TRAVEL", name="Travel Reimbursement",
                amount=rng.choice([40000, 60000, 80000]),
                kind="reimbursement", taxable=False, ssb_eligible=False,
            )

        # tax declaration: dependants grow with grade
        spouse = grade >= 3 and rng.random() < 0.55
        children = 0 if grade == 1 else rng.randint(0, min(3, grade - 1))
        parents = 1 if rng.random() < 0.3 else 0
        db.upsert_tax_declaration(
            eid, tax_year,
            spouse_claimed=spouse,
            children_count=children,
            parents_count=parents,
            insurance_premium=rng.choice([0, 100000, 200000, 300000]) if grade >= 3 else 0,
            savings=0,
        )
        if spouse:
            db.add_dependent(eid, "spouse", f"{rng.choice(FIRST_NAMES)} {rng.choice(LAST_NAMES)}")
        for c in range(children):
            db.add_dependent(eid, "child", f"Child {c + 1}", f"{2010 + c}-0{rng.randint(1, 9)}-15")
        for p in range(parents):
            db.add_dependent(eid, "parent", f"Parent {p + 1}")

    # attendance for the current month
    today = _dt.date.today()
    first = today.replace(day=1)
    rng2 = random.Random(seed + 1)
    statuses = (["present"] * 18) + ["leave", "half_day", "unpaid_leave", "absent"]
    for emp in db.employees(active_only=True):
        for offset in range(0, 26):
            day = first + _dt.timedelta(days=offset)
            if day > today or day.weekday() >= 5:
                continue
            status = rng2.choice(statuses)
            ot = rng2.choice([0, 0, 0, 1, 2, 3]) if status == "present" else 0
            db.upsert_attendance(
                emp["id"], day.isoformat(), status=status, ot_hours=float(ot),
                notes="" if status == "present" else f"demo {status}",
            )
    return created


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--count", type=int, default=24)
    ap.add_argument("--db", default=os.path.join(HERE, "payroll.db"))
    args = ap.parse_args()
    n = seed_demo(args.db, count=args.count)
    print(f"seeded {n} employees into {args.db}")
