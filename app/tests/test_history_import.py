import datetime as dt
from decimal import Decimal

from timelog import calc, export_finance, history_import as hi
from timelog.models import (
    APPROVED, KIND_ONCALL, KIND_OVERTIME, TIL, OnCallDay, OvertimeEntry, PayrollRun, Sheet, User,
)


def _approved(db, user, kind, y, m):
    now = dt.datetime(2026, m + 1 if m < 12 else 12, 3, 12, 0)
    sh = Sheet(user_id=user.id, kind=kind, year=y, month=m, seq=1, status=APPROVED, payroll_year=y, payroll_month=m,
               snapshot_grade_code="STD", snapshot_is_expert=user.is_expert, submitted_at=now, decided_at=now)
    db.add(sh)
    db.flush()
    return sh


def _seed(db, users):
    bob, alice = users["bob"], users["alice"]           # bob = Expert, alice = normal
    sh = _approved(db, bob, KIND_ONCALL, 2026, 3)
    for day, dtype, rate, inter in ((2, "W", 45, False), (7, "SAT", 70, True), (8, "SUN", 70, False)):
        r = Decimal(rate)
        sh.oncall_days.append(OnCallDay(date=dt.date(2026, 3, day), intervention=inter, day_type=dtype, rate=r,
                                        amount=calc.oncall_day_amount(r, True, inter)))
    ot = _approved(db, alice, KIND_OVERTIME, 2026, 3)
    for day, dtype, s0, e0, night, comp, credit in (
            (3, "W", 1140, 1260, "1.00", "PAID", "0"), (7, "SAT", 600, 720, "0", "PAID", "0"),
            (10, "W", 1320, 1380, "1.00", "TIL", "1.80")):
        ot.overtime_entries.append(OvertimeEntry(
            date=dt.date(2026, 3, day), start_minute=s0, end_minute=e0, hours=calc.minutes_to_hours(e0 - s0),
            night_hours=Decimal(night), day_type=dtype, compensation=comp, til_credit=Decimal(credit)))
    db.commit()


def _wipe(db):
    db.query(OnCallDay).delete()
    db.query(OvertimeEntry).delete()
    db.query(Sheet).delete()
    db.commit()


def test_round_trip_of_the_finance_exports(browser, db, users):
    _seed(db, users)
    admin = db.query(User).filter_by(username="admin").one()
    oc_file = export_finance.build_oncall_export(db, admin, 2026, 3)
    ot_file = export_finance.build_overtime_export(db, admin, 2026, 3)
    want_days = {d.date: (d.rate, d.amount, d.intervention) for d in db.query(OnCallDay)}
    _wipe(db)

    recs, _ = hi.parse_oncall(oc_file)
    assert [(r.name, len(r.days)) for r in recs] == [("Bob Test", 3)]
    assert recs[0].days[dt.date(2026, 3, 7)] == (Decimal("70"), True)

    b = browser("admin")
    files = {"oncall_file": ("oc.xlsx", oc_file), "overtime_file": ("ot.xlsx", ot_file)}
    dry = b.post("/admin/import", data={"dry_run": "1"}, files=files)
    assert dry.status_code == 200 and "nothing was saved" in dry.text and db.query(Sheet).count() == 0

    done = b.post("/admin/import", data={}, files=files)
    assert done.status_code == 200 and "Imported" in done.text
    db.expire_all()
    got = {d.date: (d.rate, d.amount, d.intervention) for d in db.query(OnCallDay)}
    assert got == want_days                                # rates, doubled expert intervention amount, flags
    sheets = db.query(Sheet).all()
    assert len(sheets) == 2 and all(s.status == APPROVED and s.sent_at for s in sheets)
    run = db.query(PayrollRun).one()
    assert (run.year, run.month, run.oncall_sheets, run.overtime_sheets, run.employees) == (2026, 3, 1, 1, 2)
    t = calc.overtime_totals(db.query(OvertimeEntry).all())
    assert t.hours_by_type["W"] == Decimal("3.00") and t.hours_by_type["SAT"] == Decimal("2.00")
    assert t.night_by_type["W"] == Decimal("2.00") and t.til_hours == Decimal("1.00") and t.til_credit == Decimal("1.80")
    assert t.paid_hours == Decimal("4.00")

    # running it again changes nothing: existing sheets are skipped
    again = b.post("/admin/import", data={}, files=files)
    assert "Already has" in again.text and db.query(Sheet).count() == 2


def test_unknown_users_experts_and_bad_files_are_skipped_or_refused(browser, db, users):
    _seed(db, users)
    admin = db.query(User).filter_by(username="admin").one()
    ot_file = export_finance.build_overtime_export(db, admin, 2026, 3)
    _wipe(db)
    users["alice"].first_name = "Zed"                     # no longer matches the name in the file
    db.commit()
    b = browser("admin")
    r = b.post("/admin/import", data={}, files={"overtime_file": ("ot.xlsx", ot_file)})
    assert "No user with this name" in r.text and db.query(Sheet).count() == 0
    assert "readable .xlsx" in b.post("/admin/import", data={}, files={"oncall_file": ("x.xlsx", b"nope")}).text
    assert "at least one" in b.post("/admin/import", data={}).text
    assert browser("alice").post("/admin/import", data={}).status_code in (403, 404)


def _merge_months(files):
    """One workbook with one sheet per month (values only), like a yearly workbook kept by hand."""
    import io

    from openpyxl import Workbook, load_workbook
    out = Workbook()
    out.remove(out.active)
    for data in files:
        src = load_workbook(io.BytesIO(data))
        ws = next(w for w in src.worksheets if w.title not in ("Support", "READ FIRST"))
        dst = out.create_sheet(ws.title)
        for row in ws.iter_rows():
            for c in row:
                if c.value is not None:
                    dst.cell(c.row, c.column).value = c.value if not hasattr(c.value, "__iter__") or isinstance(c.value, str) else str(c.value)
    buf = io.BytesIO()
    out.save(buf)
    return buf.getvalue()


def test_one_workbook_with_a_sheet_per_month(browser, db, users):
    _seed(db, users)                                             # March sheets
    bob, alice = users["bob"], users["alice"]
    sh = _approved(db, bob, KIND_ONCALL, 2026, 4)
    sh.oncall_days.append(OnCallDay(date=dt.date(2026, 4, 4), intervention=False, day_type="SAT", rate=Decimal(70), amount=Decimal(70)))
    ot = _approved(db, alice, KIND_OVERTIME, 2026, 4)
    ot.overtime_entries.append(OvertimeEntry(date=dt.date(2026, 4, 6), start_minute=1080, end_minute=1200, hours=Decimal("2.00"),
                                             night_hours=Decimal(0), day_type="W"))
    db.commit()
    admin = db.query(User).filter_by(username="admin").one()
    oc = _merge_months([export_finance.build_oncall_export(db, admin, 2026, m) for m in (3, 4)])
    ot_file = _merge_months([export_finance.build_overtime_export(db, admin, 2026, m) for m in (3, 4)])
    want_days = db.query(OnCallDay).count()
    want_hours = sum(e.hours for e in db.query(OvertimeEntry))
    _wipe(db)

    recs, _ = hi.parse_oncall(oc)
    assert sorted((r.year, r.month) for r in recs) == [(2026, 3), (2026, 4)]
    b = browser("admin")
    r = b.post("/admin/import", data={}, files={"oncall_file": ("year.xlsx", oc), "overtime_file": ("year-ot.xlsx", ot_file)})
    assert r.status_code == 200 and "Imported" in r.text
    db.expire_all()
    assert db.query(OnCallDay).count() == want_days and sum(e.hours for e in db.query(OvertimeEntry)) == want_hours
    assert {(s.year, s.month, s.kind) for s in db.query(Sheet)} == {(2026, 3, "oncall"), (2026, 4, "oncall"), (2026, 3, "overtime"), (2026, 4, "overtime")}
    assert db.query(PayrollRun).count() == 2                      # one run per month (payroll period)
