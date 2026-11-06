"""Entry point for the Myanmar HR Payroll System web dashboard.

    python3 run.py                    # http://127.0.0.1:5000
    python3 run.py --port 8080
    python3 run.py --seed             # load demo data first
    python3 run.py --db /path/to.db   # use a specific database
"""

from __future__ import annotations

import argparse
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)


def main() -> int:
    ap = argparse.ArgumentParser(description="Myanmar HR Payroll System")
    ap.add_argument("--host", default=os.environ.get("PAYROLL_HOST", "127.0.0.1"))
    ap.add_argument("--port", type=int, default=int(os.environ.get("PAYROLL_PORT", "5000")))
    ap.add_argument("--db", default=os.environ.get("PAYROLL_DB"))
    ap.add_argument("--seed", action="store_true", help="load demo data before starting")
    ap.add_argument("--seed-count", type=int, default=24, help="demo employees to create")
    ap.add_argument("--debug", action="store_true")
    args = ap.parse_args()

    if args.seed:
        from seed import seed_demo

        db_path = args.db or os.path.join(HERE, "payroll.db")
        n = seed_demo(db_path, count=args.seed_count)
        print(f"  seeded {n} demo employees -> {db_path}")

    from webapp.app import create_app

    app = create_app(args.db)
    print(f"  Myanmar HR Payroll System -> http://{args.host}:{args.port}")
    print("  routes: /  /employees  /attendance  /payroll  /rules  /healthz")
    app.run(host=args.host, port=args.port, debug=args.debug)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
