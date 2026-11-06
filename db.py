"""SQLite persistence layer for the Myanmar HR Payroll System.

Money columns are stored as TEXT holding an exact ``Decimal`` string. Storing
kyat as REAL would silently corrupt payroll figures through binary float
rounding, which is unacceptable for statutory filings.

The schema is intentionally denormalised for payslips: a payslip is a
*point-in-time legal record* and must not drift when an employee's master
data or a rule set changes later.
"""

from __future__ import annotations

import datetime as _dt
import os
import sqlite3
from contextlib import contextmanager
from decimal import Decimal
from typing import Any, Iterable, Iterator, Optional

from .money import D, ZERO

DEFAULT_DB_PATH = os.environ.get(
    "PAYROLL_DB", os.path.join(os.path.dirname(__file__), "..", "payroll.db")
)

SCHEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS employee (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    code            TEXT NOT NULL UNIQUE,
    name            TEXT NOT NULL,
    nrc             TEXT DEFAULT '',
    email           TEXT DEFAULT '',
    phone           TEXT DEFAULT '',
    department      TEXT DEFAULT '',
    position        TEXT DEFAULT '',
    join_date       TEXT NOT NULL,
    status          TEXT NOT NULL DEFAULT 'active',
    residency       TEXT NOT NULL DEFAULT 'resident',
    bank_account    TEXT DEFAULT '',
    created_at      TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS employee_dependent (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    employee_id INTEGER NOT NULL REFERENCES employee(id) ON DELETE CASCADE,
    kind        TEXT NOT NULL CHECK (kind IN ('spouse','child','parent')),
    name        TEXT NOT NULL DEFAULT '',
    dob         TEXT DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_dep_emp ON employee_dependent(employee_id);

CREATE TABLE IF NOT EXISTS tax_declaration (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    employee_id       INTEGER NOT NULL REFERENCES employee(id) ON DELETE CASCADE,
    tax_year          TEXT NOT NULL,
    spouse_claimed    INTEGER NOT NULL DEFAULT 0,
    children_count    INTEGER NOT NULL DEFAULT 0,
    parents_count     INTEGER NOT NULL DEFAULT 0,
    insurance_premium TEXT NOT NULL DEFAULT '0',
    savings           TEXT NOT NULL DEFAULT '0',
    UNIQUE (employee_id, tax_year)
);

CREATE TABLE IF NOT EXISTS salary_component (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    employee_id INTEGER NOT NULL REFERENCES employee(id) ON DELETE CASCADE,
    code        TEXT NOT NULL,
    name        TEXT NOT NULL,
    amount      TEXT NOT NULL DEFAULT '0',
    kind        TEXT NOT NULL DEFAULT 'earning'
                CHECK (kind IN ('earning','deduction','reimbursement')),
    taxable     INTEGER NOT NULL DEFAULT 1,
    ssb_eligible INTEGER NOT NULL DEFAULT 1,
    recurring   INTEGER NOT NULL DEFAULT 1
);
CREATE INDEX IF NOT EXISTS idx_comp_emp ON salary_component(employee_id);

CREATE TABLE IF NOT EXISTS attendance (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    employee_id INTEGER NOT NULL REFERENCES employee(id) ON DELETE CASCADE,
    work_date   TEXT NOT NULL,
    status      TEXT NOT NULL DEFAULT 'present'
                CHECK (status IN ('present','half_day','leave','unpaid_leave','absent')),
    ot_hours    REAL NOT NULL DEFAULT 0,
    notes       TEXT DEFAULT '',
    UNIQUE (employee_id, work_date)
);
CREATE INDEX IF NOT EXISTS idx_att_emp_date ON attendance(employee_id, work_date);

CREATE TABLE IF NOT EXISTS payroll_run (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    period       TEXT NOT NULL,
    status       TEXT NOT NULL DEFAULT 'draft'
                 CHECK (status IN ('draft','approved','locked')),
    rule_key     TEXT NOT NULL DEFAULT '',
    created_at   TEXT NOT NULL DEFAULT (datetime('now')),
    approved_at  TEXT,
    notes        TEXT DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_run_period ON payroll_run(period);

CREATE TABLE IF NOT EXISTS payslip (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id          INTEGER NOT NULL REFERENCES payroll_run(id) ON DELETE CASCADE,
    employee_id     INTEGER NOT NULL REFERENCES employee(id),
    employee_code   TEXT NOT NULL,
    employee_name   TEXT NOT NULL,
    period          TEXT NOT NULL,
    rule_key        TEXT NOT NULL DEFAULT '',
    gross_earnings  TEXT NOT NULL DEFAULT '0',
    total_deductions TEXT NOT NULL DEFAULT '0',
    net_pay         TEXT NOT NULL DEFAULT '0',
    annual_gross    TEXT NOT NULL DEFAULT '0',
    taxable_income  TEXT NOT NULL DEFAULT '0',
    basic_relief    TEXT NOT NULL DEFAULT '0',
    allowances_total TEXT NOT NULL DEFAULT '0',
    pit_annual      TEXT NOT NULL DEFAULT '0',
    pit_monthly     TEXT NOT NULL DEFAULT '0',
    ssb_base        TEXT NOT NULL DEFAULT '0',
    ssb_employee    TEXT NOT NULL DEFAULT '0',
    ssb_employer    TEXT NOT NULL DEFAULT '0',
    reimbursements  TEXT NOT NULL DEFAULT '0',
    breakdown_json  TEXT NOT NULL DEFAULT '[]',
    UNIQUE (run_id, employee_id)
);
CREATE INDEX IF NOT EXISTS idx_slip_run ON payslip(run_id);
"""


class Database:
    """Thin SQLite wrapper with dict-shaped access."""

    def __init__(self, path: str | os.PathLike = DEFAULT_DB_PATH):
        self.path = str(path)
        self._ensure_dir()
        with self.connect() as conn:
            conn.executescript(SCHEMA)

    def _ensure_dir(self) -> None:
        parent = os.path.dirname(os.path.abspath(self.path))
        os.makedirs(parent, exist_ok=True)

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    # -- generic helpers ------------------------------------------------

    def query(self, sql: str, params: Iterable[Any] = ()) -> list[dict]:
        with self.connect() as conn:
            rows = conn.execute(sql, tuple(params)).fetchall()
            return [dict(r) for r in rows]

    def one(self, sql: str, params: Iterable[Any] = ()) -> Optional[dict]:
        with self.connect() as conn:
            row = conn.execute(sql, tuple(params)).fetchone()
            return dict(row) if row else None

    def execute(self, sql: str, params: Iterable[Any] = ()) -> int:
        with self.connect() as conn:
            cur = conn.execute(sql, tuple(params))
            return cur.lastrowid if cur.lastrowid else cur.rowcount

    def executemany(self, sql: str, seq: Iterable[Iterable[Any]]) -> None:
        with self.connect() as conn:
            conn.executemany(sql, [tuple(p) for p in seq])

    # -- employees ------------------------------------------------------

    def add_employee(self, **fields) -> int:
        cols = (
            "code name nrc email phone department position join_date "
            "status residency bank_account"
        ).split()
        data = {c: fields.get(c, "") for c in cols}
        if not data.get("status"):
            data["status"] = "active"
        if not data.get("residency"):
            data["residency"] = "resident"
        if data["status"] not in {"active", "inactive"}:
            raise ValueError(f"invalid status: {data['status']}")
        if data["residency"] not in {"resident", "non_resident"}:
            raise ValueError(f"invalid residency: {data['residency']}")
        if not data.get("code") or not data.get("name"):
            raise ValueError("employee requires code and name")
        if not data.get("join_date"):
            raise ValueError("employee requires join_date")
        placeholders = ", ".join("?" for _ in cols)
        with self.connect() as conn:
            cur = conn.execute(
                f"INSERT INTO employee ({', '.join(cols)}) VALUES ({placeholders})",
                tuple(str(data[c]) for c in cols),
            )
            return int(cur.lastrowid)

    def employees(self, active_only: bool = False) -> list[dict]:
        sql = "SELECT * FROM employee"
        if active_only:
            sql += " WHERE status = 'active'"
        sql += " ORDER BY code"
        return self.query(sql)

    def employee(self, employee_id: int) -> Optional[dict]:
        return self.one("SELECT * FROM employee WHERE id = ?", (employee_id,))

    def employee_by_code(self, code: str) -> Optional[dict]:
        return self.one("SELECT * FROM employee WHERE code = ?", (code,))

    def update_employee(self, employee_id: int, **fields) -> None:
        allowed = {
            "code", "name", "nrc", "email", "phone", "department",
            "position", "join_date", "status", "residency", "bank_account",
        }
        sets, vals = [], []
        for k, v in fields.items():
            if k in allowed:
                sets.append(f"{k} = ?")
                vals.append(str(v))
        if not sets:
            return
        vals.append(employee_id)
        with self.connect() as conn:
            conn.execute(f"UPDATE employee SET {', '.join(sets)} WHERE id = ?", vals)

    def headcount(self, active_only: bool = True) -> int:
        sql = "SELECT COUNT(*) AS n FROM employee"
        if active_only:
            sql += " WHERE status = 'active'"
        return int(self.one(sql)["n"])

    # -- dependents / tax ----------------------------------------------

    def dependents(self, employee_id: int) -> list[dict]:
        return self.query(
            "SELECT * FROM employee_dependent WHERE employee_id = ? ORDER BY kind, id",
            (employee_id,),
        )

    def add_dependent(self, employee_id: int, kind: str, name: str = "", dob: str = "") -> int:
        if kind not in {"spouse", "child", "parent"}:
            raise ValueError("kind must be spouse|child|parent")
        return self.execute(
            "INSERT INTO employee_dependent (employee_id, kind, name, dob) VALUES (?,?,?,?)",
            (employee_id, kind, name, dob),
        )

    def clear_dependents(self, employee_id: int) -> None:
        self.execute("DELETE FROM employee_dependent WHERE employee_id = ?", (employee_id,))

    def tax_declaration(self, employee_id: int, tax_year: str) -> Optional[dict]:
        return self.one(
            "SELECT * FROM tax_declaration WHERE employee_id = ? AND tax_year = ?",
            (employee_id, tax_year),
        )

    def upsert_tax_declaration(self, employee_id: int, tax_year: str, **fields) -> None:
        cols = ("spouse_claimed", "children_count", "parents_count",
                "insurance_premium", "savings")
        vals = {
            "spouse_claimed": int(bool(fields.get("spouse_claimed", 0))),
            "children_count": int(fields.get("children_count", 0)),
            "parents_count": int(fields.get("parents_count", 0)),
            "insurance_premium": str(D(fields.get("insurance_premium", 0))),
            "savings": str(D(fields.get("savings", 0))),
        }
        with self.connect() as conn:
            conn.execute(
                """INSERT INTO tax_declaration
                   (employee_id, tax_year, {c}) VALUES (?, ?, {p})
                   ON CONFLICT(employee_id, tax_year) DO UPDATE SET {u}""".format(
                    c=", ".join(cols),
                    p=", ".join("?" for _ in cols),
                    u=", ".join(f"{c} = excluded.{c}" for c in cols),
                ),
                (employee_id, tax_year, *(vals[c] for c in cols)),
            )

    # -- salary components ----------------------------------------------

    def components(self, employee_id: int) -> list[dict]:
        return self.query(
            "SELECT * FROM salary_component WHERE employee_id = ? ORDER BY id",
            (employee_id,),
        )

    def add_component(self, employee_id: int, **fields) -> int:
        return self.execute(
            """INSERT INTO salary_component
               (employee_id, code, name, amount, kind, taxable, ssb_eligible, recurring)
               VALUES (?,?,?,?,?,?,?,?)""",
            (
                employee_id,
                fields.get("code", "PAY"),
                fields.get("name", "Payment"),
                str(D(fields.get("amount", 0))),
                fields.get("kind", "earning"),
                int(bool(fields.get("taxable", True))),
                int(bool(fields.get("ssb_eligible", True))),
                int(bool(fields.get("recurring", True))),
            ),
        )

    def delete_component(self, component_id: int) -> None:
        self.execute("DELETE FROM salary_component WHERE id = ?", (component_id,))

    # -- attendance -----------------------------------------------------

    def attendance_between(
        self, date_from: str, date_to: str, employee_id: Optional[int] = None
    ) -> list[dict]:
        sql = "SELECT * FROM attendance WHERE work_date BETWEEN ? AND ?"
        params: list[Any] = [date_from, date_to]
        if employee_id is not None:
            sql += " AND employee_id = ?"
            params.append(employee_id)
        sql += " ORDER BY employee_id, work_date"
        return self.query(sql, params)

    def upsert_attendance(
        self,
        employee_id: int,
        work_date: str,
        status: str = "present",
        ot_hours: float = 0.0,
        notes: str = "",
    ) -> None:
        if status not in {"present", "half_day", "leave", "unpaid_leave", "absent"}:
            raise ValueError(f"invalid attendance status: {status}")
        with self.connect() as conn:
            conn.execute(
                """INSERT INTO attendance (employee_id, work_date, status, ot_hours, notes)
                   VALUES (?,?,?,?,?)
                   ON CONFLICT(employee_id, work_date) DO UPDATE SET
                     status = excluded.status,
                     ot_hours = excluded.ot_hours,
                     notes = excluded.notes""",
                (employee_id, work_date, status, float(ot_hours), notes),
            )

    # -- payroll runs ----------------------------------------------------

    def create_run(self, period: str, rule_key: str = "", notes: str = "") -> int:
        with self.connect() as conn:
            cur = conn.execute(
                "INSERT INTO payroll_run (period, rule_key, notes) VALUES (?,?,?)",
                (period, rule_key, notes),
            )
            return int(cur.lastrowid)

    def runs(self) -> list[dict]:
        return self.query(
            """SELECT r.*,
                      (SELECT COUNT(*) FROM payslip p WHERE p.run_id = r.id) AS slip_count,
                      (SELECT COALESCE(SUM(CAST(p.net_pay AS REAL)),0) FROM payslip p
                        WHERE p.run_id = r.id) AS total_net
               FROM payroll_run r ORDER BY r.period DESC, r.id DESC"""
        )

    def run(self, run_id: int) -> Optional[dict]:
        return self.one("SELECT * FROM payroll_run WHERE id = ?", (run_id,))

    def set_run_status(self, run_id: int, status: str) -> None:
        if status not in {"draft", "approved", "locked"}:
            raise ValueError("invalid run status")
        if status == "approved":
            self.execute(
                "UPDATE payroll_run SET status = ?, approved_at = datetime('now') WHERE id = ?",
                (status, run_id),
            )
        else:
            self.execute("UPDATE payroll_run SET status = ? WHERE id = ?", (status, run_id))

    # -- payslips ---------------------------------------------------------

    def save_payslip(self, run_id: int, slip: dict) -> int:
        cols = (
            "run_id employee_id employee_code employee_name period rule_key "
            "gross_earnings total_deductions net_pay annual_gross taxable_income "
            "basic_relief allowances_total pit_annual pit_monthly ssb_base "
            "ssb_employee ssb_employer reimbursements breakdown_json"
        ).split()
        data = {c: slip.get(c, "0") for c in cols}
        data["run_id"] = run_id
        placeholders = ", ".join("?" for _ in cols)
        with self.connect() as conn:
            conn.execute(
                f"INSERT OR REPLACE INTO payslip ({', '.join(cols)}) VALUES ({placeholders})",
                tuple(str(data[c]) for c in cols),
            )
            return int(
                conn.execute(
                    "SELECT id FROM payslip WHERE run_id = ? AND employee_id = ?",
                    (run_id, data["employee_id"]),
                ).fetchone()[0]
            )

    def payslips(self, run_id: int) -> list[dict]:
        return self.query(
            "SELECT * FROM payslip WHERE run_id = ? ORDER BY employee_code", (run_id,)
        )

    def payslip(self, slip_id: int) -> Optional[dict]:
        return self.one("SELECT * FROM payslip WHERE id = ?", (slip_id,))

    def delete_payslips(self, run_id: int) -> None:
        self.execute("DELETE FROM payslip WHERE run_id = ?", (run_id,))

    # -- aggregate reporting ----------------------------------------------

    def payroll_summary(self, limit: int = 12) -> list[dict]:
        return self.query(
            """SELECT run_id, period,
                      COUNT(*) AS headcount,
                      SUM(CAST(gross_earnings AS REAL)) AS gross,
                      SUM(CAST(pit_monthly AS REAL)) AS pit,
                      SUM(CAST(ssb_employee AS REAL)) AS ssb_ee,
                      SUM(CAST(ssb_employer AS REAL)) AS ssb_er,
                      SUM(CAST(net_pay AS REAL)) AS net
               FROM payslip
               GROUP BY run_id, period
               ORDER BY period DESC LIMIT ?""",
            (limit,),
        )

    def department_breakdown(self) -> list[dict]:
        return self.query(
            """SELECT COALESCE(NULLIF(e.department,''),'(unassigned)') AS department,
                      COUNT(*) AS headcount,
                      COALESCE(SUM(CAST(s.gross_earnings AS REAL)),0) AS gross,
                      COALESCE(SUM(CAST(s.net_pay AS REAL)),0) AS net
               FROM employee e
               LEFT JOIN payslip s ON s.employee_id = e.id
                 AND s.id = (SELECT MAX(id) FROM payslip WHERE employee_id = e.id)
               WHERE e.status = 'active'
               GROUP BY 1 ORDER BY gross DESC"""
        )


def db(path: str | os.PathLike | None = None) -> Database:
    """Module-level convenience accessor."""
    global _DB
    if path is not None:
        _DB = Database(path)
    elif _DB is None:
        _DB = Database()
    return _DB


_DB: Optional[Database] = None
