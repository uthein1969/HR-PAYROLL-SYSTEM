"""Flask web dashboard for the Myanmar HR Payroll System.

Run with::

    python3 run.py            # http://127.0.0.1:5000
"""

from __future__ import annotations

import datetime as _dt
import json
import os
import sys

from flask import (
    Flask,
    flash,
    jsonify,
    redirect,
    render_template,
    request,
    url_for,
)

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from payroll.db import Database  # noqa: E402
from payroll.money import D  # noqa: E402
from payroll.rules import MYANMAR_RULES, rules_for  # noqa: E402
from payroll.service import PayrollService, month_bounds, tax_year_for  # noqa: E402

APP_DIR = os.path.dirname(os.path.abspath(__file__))


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _fmt_mmk(value) -> str:
    """Format a Decimal-ish amount as ``1,234,567``."""
    try:
        n = int(D(value))
    except Exception:
        return str(value)
    return f"{n:,}"


def _dec(value):
    """Coerce a stored string/number to ``Decimal`` for template arithmetic."""
    try:
        return D(value)
    except Exception:
        return D(0)


def _current_period() -> str:
    today = _dt.date.today()
    return f"{today.year:04d}-{today.month:02d}"


def _period_choices(limit: int = 24) -> list[str]:
    out, y, m = [], _dt.date.today().year, _dt.date.today().month
    for _ in range(limit):
        out.append(f"{y:04d}-{m:02d}")
        m -= 1
        if m == 0:
            m, y = 12, y - 1
    return out


def _tax_year_choices(count: int = 6) -> list[str]:
    """Distinct Myanmar tax years (1 Apr - 31 Mar), newest first."""
    cur = _dt.date.today()
    start = cur.year if cur.month >= 4 else cur.year - 1
    return [f"{start - i}-{start - i + 1}" for i in range(count)]


# ---------------------------------------------------------------------------
# app factory
# ---------------------------------------------------------------------------

def create_app(db_path: str | None = None) -> Flask:
    app = Flask(__name__)
    app.secret_key = os.environ.get("PAYROLL_SECRET", "dev-only-change-me")
    app.jinja_env.filters["mmk"] = _fmt_mmk
    app.jinja_env.filters["dec"] = _dec
    app.jinja_env.globals["today"] = _dt.date.today

    db = Database(db_path) if db_path else Database()
    svc = PayrollService(db)
    app.config["PAYROLL_DB"] = db
    app.config["PAYROLL_SVC"] = svc

    def get_db() -> Database:
        return app.config["PAYROLL_DB"]

    def get_svc() -> PayrollService:
        return app.config["PAYROLL_SVC"]

    # ---------------- dashboard ----------------
    @app.route("/")
    def dashboard():
        d = get_db()
        headcount = d.headcount()
        summary = d.payroll_summary(limit=6)
        depts = d.department_breakdown()
        rules = rules_for(_dt.date.today())

        latest = summary[0] if summary else None
        kpi = {
            "headcount": headcount,
            "gross": latest["gross"] if latest else 0,
            "pit": latest["pit"] if latest else 0,
            "ssb_ee": latest["ssb_ee"] if latest else 0,
            "ssb_er": latest["ssb_er"] if latest else 0,
            "net": latest["net"] if latest else 0,
        }
        # Trend for the simple CSS chart (oldest -> newest).
        trend = list(reversed(summary))
        max_gross = max((float(t["gross"] or 0) for t in trend), default=0) or 1

        return render_template(
            "dashboard.html",
            kpi=kpi,
            summary=summary,
            trend=trend,
            max_gross=max_gross,
            depts=depts,
            rules=rules,
            period=latest["period"] if latest else _current_period(),
        )

    # ---------------- employees ----------------
    @app.route("/employees")
    def employees_list():
        d = get_db()
        q = (request.args.get("q") or "").strip().lower()
        rows = d.employees()
        if q:
            rows = [
                e for e in rows
                if q in e["name"].lower() or q in e["code"].lower()
                or q in (e.get("department") or "").lower()
            ]
        return render_template("employees.html", employees=rows, q=q)

    @app.route("/employees/new", methods=["GET", "POST"])
    def employee_new():
        if request.method == "POST":
            d = get_db()
            try:
                eid = d.add_employee(
                    code=request.form.get("code", "").strip(),
                    name=request.form.get("name", "").strip(),
                    nrc=request.form.get("nrc", "").strip(),
                    email=request.form.get("email", "").strip(),
                    phone=request.form.get("phone", "").strip(),
                    department=request.form.get("department", "").strip(),
                    position=request.form.get("position", "").strip(),
                    join_date=request.form.get("join_date") or _dt.date.today().isoformat(),
                    residency=request.form.get("residency", "resident"),
                    bank_account=request.form.get("bank_account", "").strip(),
                )
                # optional starting salary
                basic = request.form.get("basic_salary", "").strip()
                if basic:
                    get_db().add_component(
                        eid, code="BASIC", name="Basic Salary", amount=basic,
                        kind="earning", taxable=True, ssb_eligible=True, recurring=True,
                    )
                flash(f"Employee {request.form.get('code')} created.", "ok")
                return redirect(url_for("employee_detail", employee_id=eid))
            except Exception as exc:
                flash(str(exc), "err")
        return render_template(
            "employee_form.html", emp=None, periods=_period_choices(),
            tax_years=_tax_year_choices(),
        )

    @app.route("/employees/<int:employee_id>")
    def employee_detail(employee_id):
        d = get_db()
        emp = d.employee(employee_id)
        if emp is None:
            flash("Employee not found", "err")
            return redirect(url_for("employees_list"))
        return render_template(
            "employee_form.html",
            emp=emp,
            components=d.components(employee_id),
            dependents=d.dependents(employee_id),
            decl=d.tax_declaration(employee_id, tax_year_for(_dt.date.today())),
            periods=_period_choices(),
            tax_years=_tax_year_choices(),
        )

    @app.route("/employees/<int:employee_id>/update", methods=["POST"])
    def employee_update(employee_id):
        d = get_db()
        d.update_employee(
            employee_id,
            code=request.form.get("code", "").strip(),
            name=request.form.get("name", "").strip(),
            nrc=request.form.get("nrc", "").strip(),
            email=request.form.get("email", "").strip(),
            phone=request.form.get("phone", "").strip(),
            department=request.form.get("department", "").strip(),
            position=request.form.get("position", "").strip(),
            join_date=request.form.get("join_date", "").strip(),
            status=request.form.get("status", "active"),
            residency=request.form.get("residency", "resident"),
            bank_account=request.form.get("bank_account", "").strip(),
        )
        d.upsert_tax_declaration(
            employee_id,
            request.form.get("tax_year") or tax_year_for(_dt.date.today()),
            spouse_claimed=bool(request.form.get("spouse_claimed")),
            children_count=int(request.form.get("children_count") or 0),
            parents_count=int(request.form.get("parents_count") or 0),
            insurance_premium=request.form.get("insurance_premium") or 0,
            savings=request.form.get("savings") or 0,
        )
        flash("Employee updated.", "ok")
        return redirect(url_for("employee_detail", employee_id=employee_id))

    @app.route("/employees/<int:employee_id>/component", methods=["POST"])
    def employee_add_component(employee_id):
        d = get_db()
        try:
            d.add_component(
                employee_id,
                code=request.form.get("code", "PAY").strip() or "PAY",
                name=request.form.get("name", "").strip() or "Payment",
                amount=request.form.get("amount") or 0,
                kind=request.form.get("kind", "earning"),
                taxable=bool(request.form.get("taxable")),
                ssb_eligible=bool(request.form.get("ssb_eligible")),
                recurring=bool(request.form.get("recurring")),
            )
            flash("Component added.", "ok")
        except Exception as exc:
            flash(str(exc), "err")
        return redirect(url_for("employee_detail", employee_id=employee_id))

    @app.route("/employees/<int:employee_id>/component/<int:cid>/delete", methods=["POST"])
    def employee_delete_component(employee_id, cid):
        get_db().delete_component(cid)
        flash("Component removed.", "ok")
        return redirect(url_for("employee_detail", employee_id=employee_id))

    @app.route("/employees/<int:employee_id>/dependent", methods=["POST"])
    def employee_add_dependent(employee_id):
        d = get_db()
        try:
            d.add_dependent(
                employee_id,
                request.form.get("kind", "child"),
                request.form.get("name", "").strip(),
                request.form.get("dob", "").strip(),
            )
            flash("Dependent added.", "ok")
        except Exception as exc:
            flash(str(exc), "err")
        return redirect(url_for("employee_detail", employee_id=employee_id))

    # ---------------- attendance ----------------
    @app.route("/attendance")
    def attendance_list():
        period = request.args.get("period") or _current_period()
        d_from, d_to = month_bounds(period)
        rows = get_svc().attendance_summary_rows(period)
        return render_template(
            "attendance.html",
            rows=rows,
            period=period,
            periods=_period_choices(),
            date_from=d_from,
            date_to=d_to,
        )

    @app.route("/attendance/save", methods=["POST"])
    def attendance_save():
        d = get_db()
        employee_id = int(request.form.get("employee_id") or 0)
        work_date = request.form.get("work_date", "").strip()
        if not employee_id or not work_date:
            flash("employee and date are required", "err")
        else:
            d.upsert_attendance(
                employee_id,
                work_date,
                status=request.form.get("status", "present"),
                ot_hours=float(request.form.get("ot_hours") or 0),
                notes=request.form.get("notes", "").strip(),
            )
            flash("Attendance saved.", "ok")
        period = request.form.get("period") or work_date[:7]
        return redirect(url_for("attendance_list", period=period))

    # ---------------- payroll ----------------
    @app.route("/payroll")
    def payroll_runs():
        return render_template("payroll_runs.html", runs=get_db().runs(),
                               periods=_period_choices())

    @app.route("/payroll/run", methods=["POST"])
    def payroll_run_create():
        period = request.form.get("period") or _current_period()
        try:
            result, run_id = get_svc().compute_run(
                period, include_attendance=bool(request.form.get("include_attendance"))
            )
            flash(
                f"Payroll run #{run_id} computed for {result.headcount} employee(s).",
                "ok",
            )
            return redirect(url_for("payroll_run_detail", run_id=run_id))
        except Exception as exc:
            flash(str(exc), "err")
            return redirect(url_for("payroll_runs"))

    @app.route("/payroll/run/<int:run_id>")
    def payroll_run_detail(run_id):
        d = get_db()
        run = d.run(run_id)
        if run is None:
            flash("Run not found", "err")
            return redirect(url_for("payroll_runs"))
        slips = d.payslips(run_id)
        totals = {
            "gross": sum(D(s["gross_earnings"]) for s in slips),
            "pit": sum(D(s["pit_monthly"]) for s in slips),
            "ssb_ee": sum(D(s["ssb_employee"]) for s in slips),
            "ssb_er": sum(D(s["ssb_employer"]) for s in slips),
            "net": sum(D(s["net_pay"]) for s in slips),
        }
        return render_template(
            "payroll_run.html", run=run, slips=slips, totals=totals
        )

    @app.route("/payroll/run/<int:run_id>/status", methods=["POST"])
    def payroll_run_status(run_id):
        d = get_db()
        run = d.run(run_id)
        if run is None:
            flash("Run not found", "err")
            return redirect(url_for("payroll_runs"))
        if run["status"] == "locked":
            flash("This run is locked and cannot be changed.", "err")
            return redirect(url_for("payroll_run_detail", run_id=run_id))
        status = request.form.get("status", "approved")
        try:
            d.set_run_status(run_id, status)
            flash(f"Run marked {status}.", "ok")
        except Exception as exc:
            flash(str(exc), "err")
        return redirect(url_for("payroll_run_detail", run_id=run_id))

    @app.route("/payslip/<int:slip_id>")
    def payslip_detail(slip_id):
        d = get_db()
        slip = d.payslip(slip_id)
        if slip is None:
            flash("Payslip not found", "err")
            return redirect(url_for("payroll_runs"))
        breakdown = json.loads(slip.get("breakdown_json") or "[]")
        return render_template("payslip.html", slip=slip, breakdown=breakdown)

    # ---------------- rules ----------------
    @app.route("/rules")
    def rules_view():
        return render_template("rules.html", rule_sets=MYANMAR_RULES,
                               active=rules_for(_dt.date.today()))

    # ---------------- api ----------------
    @app.route("/api/overview.json")
    def api_overview():
        d = get_db()
        summary = d.payroll_summary(limit=12)
        return jsonify(
            {
                "headcount": d.headcount(),
                "periods": [
                    {
                        "period": s["period"],
                        "headcount": s["headcount"],
                        "gross": float(s["gross"] or 0),
                        "pit": float(s["pit"] or 0),
                        "net": float(s["net"] or 0),
                    }
                    for s in summary
                ],
            }
        )

    @app.route("/healthz")
    def healthz():
        return jsonify({"ok": True, "headcount": get_db().headcount()})

    return app
app = create_app()