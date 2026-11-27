#!/usr/bin/env python3
"""Generate sample Finance workbooks (on-call + overtime, one pair per payroll month) to test Admin > Import History.

    app/.venv/bin/python scripts/make-backfill-samples.py [outdir] [--year 2026] [--months 10] [--db demo.db]

It builds a throwaway SQLite database with the demo users (Helen Head, Mark Manager, Alice Normal, Bob Expert, Carol Normal,
Dave Other), fills approved on-call and overtime sheets and writes the exports with the application's own exporter, so the
files have exactly the layout of the Payroll page downloads. To import them, create the same users in the target instance:
    docker compose exec app python -m timelog.demo
Everything is fictional; *.xlsx is git-ignored. Secrets are random per run (the throwaway database is not kept)."""
import argparse
import datetime as dt
import os
import sys
import tempfile
from decimal import Decimal
from pathlib import Path

ap = argparse.ArgumentParser()
ap.add_argument("outdir", nargs="?", default="samples/backfill")
ap.add_argument("--year", type=int, default=dt.date.today().year)
ap.add_argument("--months", type=int, default=10)
ap.add_argument("--db", default="")
args = ap.parse_args()

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "app"))
db_file = Path(args.db) if args.db else Path(tempfile.mkdtemp()) / "samples.db"
import secrets  # noqa: E402

# throwaway database: random secrets generated on every run, nothing credential-like in the source
demo_password = secrets.token_urlsafe(12)
env = {"DATABASE_URL": f"sqlite:///{db_file}", "SECRET_KEY": secrets.token_urlsafe(32), "SESSION_COOKIE_SECURE": "false",
       "BOOTSTRAP_ADMIN_USERNAME": "admin", "BOOTSTRAP_ADMIN_PASSWORD": demo_password, "BACKUP_SCHEDULER": "false"}
os.environ.update(env)
sys.argv = ["demo", demo_password]

from _fake import Fake  # noqa: E402
from sqlalchemy import select  # noqa: E402

from timelog import calc, demo, export_finance, workflow  # noqa: E402
from timelog.calendar_utils import day_type, holiday_set, month_days  # noqa: E402
from timelog.config import get_settings  # noqa: E402
from timelog.db import SessionLocal  # noqa: E402
from timelog.main import create_app  # noqa: E402
from timelog.models import APPROVED, KIND_ONCALL, KIND_OVERTIME, TIL, OnCallDay, OvertimeEntry, Sheet, User  # noqa: E402

create_app()
demo.main()
rnd = Fake(args.year)
settings = get_settings()


def approved(db, user, kind, y, m):
    when = dt.datetime(y, m, 1) + dt.timedelta(days=32)
    when = when.replace(day=3, hour=10)
    sh = Sheet(user_id=user.id, kind=kind, year=y, month=m, seq=1, status=APPROVED, payroll_year=y, payroll_month=m,
               snapshot_grade_code="STD", snapshot_is_expert=user.is_expert, submitted_at=when, decided_at=when,
               employee_comment="Sample")
    sh.user = user
    db.add(sh)
    db.flush()
    return sh


with SessionLocal() as db:
    u = {x.username: x for x in db.scalars(select(User))}
    factors = workflow.til_factors(db)
    out = Path(args.outdir)
    out.mkdir(parents=True, exist_ok=True)
    for m in range(1, args.months + 1):
        days = month_days(args.year, m)
        hol = holiday_set(db, days[0], days[-1])
        # on-call: rotation weeks alternate between Bob (Expert, with interventions) and Alice / Carol
        rot = [(u["bob"], True), (u["alice"], False), (u["bob"], True), (u["carol"], False)]
        week = 0
        by_user = {}
        for d in days:
            if d.weekday() == 4 and d != days[0]:
                week += 1
            user, expert = rot[(week + m) % 4]
            by_user.setdefault(user.id, (user, []))[1].append(d)
        for user, dts in by_user.values():
            sh = approved(db, user, KIND_ONCALL, args.year, m)
            grade = workflow.grade_of(db, user)
            for d in dts:
                inter = user.is_expert and rnd.random() < .22
                dtp = day_type(d, hol)
                rate = grade.rate_for(dtp)
                sh.oncall_days.append(OnCallDay(date=d, intervention=inter, day_type=dtp, rate=rate,
                                                amount=calc.oncall_day_amount(rate, user.is_expert, inter),
                                                incident_ref=f"INC{rnd.randint(100000, 999999)}" if inter else ""))
        # overtime: Alice, Carol (paid, some TIL) and Dave
        for who in ("alice", "carol", "dave"):
            if rnd.random() < .15:
                continue
            sh = approved(db, u[who], KIND_OVERTIME, args.year, m)
            used = set()
            for _ in range(rnd.randint(2, 7)):
                d = rnd.choice(days)
                start = rnd.choice([6 * 60, 7 * 60, 18 * 60, 19 * 60, 20 * 60, 21 * 60 + 30, 22 * 60]) if d.weekday() < 5 \
                    else rnd.choice([8 * 60, 10 * 60, 14 * 60])
                end = min(1440, start + rnd.choice([60, 90, 120, 150, 180, 240]))
                if any(abs(start - s2) < 300 for dd, s2 in used if dd == d):
                    continue
                used.add((d, start))
                e = OvertimeEntry(date=d, start_minute=start, end_minute=end,
                                  compensation=TIL if rnd.random() < .2 else "PAID",
                                  incident_ref=f"INC{rnd.randint(100000, 999999)}", description="Out-of-hours intervention (sample)",
                                  hours=Decimal(0), night_hours=Decimal(0), day_type="W", til_credit=Decimal(0))
                workflow._compute_entry(settings, e, day_type(d, hol), factors)
                sh.overtime_entries.append(e)
    db.commit()
    admin = db.scalar(select(User).where(User.username == "admin"))
    for m in range(1, args.months + 1):
        tag = f"Y{args.year % 100:02d}M{m:02d}_IT"
        (out / f"{tag}_OnCall.xlsx").write_bytes(export_finance.build_oncall_export(db, admin, args.year, m))
        (out / f"{tag}_Overtime.xlsx").write_bytes(export_finance.build_overtime_export(db, admin, args.year, m))

# the same data as ONE workbook per kind with a sheet per month (values only), like a yearly workbook kept by hand
from openpyxl import Workbook, load_workbook  # noqa: E402


def merge(kind: str) -> None:
    book = Workbook()
    book.remove(book.active)
    for m in range(1, args.months + 1):
        src = load_workbook(out / f"Y{args.year % 100:02d}M{m:02d}_IT_{kind}.xlsx")
        ws = next(w for w in src.worksheets if w.title not in ("Support", "READ FIRST"))
        dst = book.create_sheet(ws.title)
        for row in ws.iter_rows():
            for c in row:
                if c.value is not None:
                    dst.cell(c.row, c.column).value = c.value
    (out / "all-months").mkdir(exist_ok=True)
    book.save(out / "all-months" / f"Y{args.year % 100:02d}_IT_{kind}_all-months.xlsx")


merge("OnCall")
merge("Overtime")
print(f"{args.months * 2} workbooks in {out} (+ 2 yearly workbooks with one sheet per month in {out}/all-months); database {db_file}")
