import datetime as dt
import io

from openpyxl import load_workbook

from timelog.access import can_view_user, subordinate_ids
from timelog.ldap_auth import LdapProfile, sync_user
from timelog.models import Sheet, User


def _oncall_form(days, interventions=()):
    data = {"action": "save"}
    for d in days:
        data[f"oc_2026-08-{d:02d}"] = "1"
    for d in interventions:
        data[f"iv_2026-08-{d:02d}"] = "1"
    return data


def _ot(date, start, end, **extra):
    return {"date": date, "start": start, "end": end, "description": extra.pop("description", "Work"), **extra}


def _overtime_sheets(db, user):
    db.expire_all()
    return db.query(Sheet).filter_by(user_id=user.id, kind="overtime").order_by(Sheet.seq).all()


def test_login_required_and_bad_password(browser, users):
    b = browser()
    r = b.get("/", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].startswith("/login")
    assert b.login("alice", "wrong-password").status_code == 401


def test_csrf_required(browser, users):
    b = browser("alice")
    r = b.post("/oncall/2026/8", {**_oncall_form([10]), "csrf_token": "bogus"})
    assert r.status_code == 400


def test_hierarchy_visibility(db, users):
    assert subordinate_ids(db, users["head"]) == {users[n].id for n in ("manager", "alice", "bob", "carol", "dave")}
    assert can_view_user(db, users["manager"], users["alice"])
    assert not can_view_user(db, users["alice"], users["carol"])  # colleagues
    assert not can_view_user(db, users["manager"], users["dave"])  # other branch


def test_oncall_expert_flow_and_approval(browser, db, users):
    bob = browser("bob")
    # 8 Aug Sat (70), 9 Aug Sun (70) with intervention -> 140, 15 Aug holiday (90)
    r = bob.post("/oncall/2026/8", {**_oncall_form([8, 9, 15], [9]), "action": "submit"})
    assert r.status_code == 303
    sheet = db.query(Sheet).filter_by(user_id=users["bob"].id).one()
    db.refresh(sheet)
    assert sheet.status == "submitted"
    assert sum(d.amount for d in sheet.oncall_days) == 70 + 140 + 90
    assert {d.day_type for d in sheet.oncall_days} == {"SAT", "SUN", "H"}

    # Colleague cannot see it (404), editing after submit is refused
    assert browser("alice").get(f"/oncall/2026/8?user_id={users['bob'].id}").status_code == 404
    bob.post("/oncall/2026/8", _oncall_form([10]))
    db.refresh(sheet)
    assert len(sheet.oncall_days) == 3

    # A peer cannot approve; the upper manager can view (and, skip-level, approve: see test_skip_level_approval)
    head = browser("head")
    assert head.get(f"/oncall/2026/8?user_id={users['bob'].id}").status_code == 200
    browser("carol").post(f"/sheets/{sheet.id}/decide", {"action": "approve"})
    db.refresh(sheet)
    assert sheet.status == "submitted"
    mgr = browser("manager")
    mgr.post(f"/sheets/{sheet.id}/decide", {"action": "reject"})
    db.refresh(sheet)
    assert sheet.status == "submitted"  # rejection needs a comment
    mgr.post(f"/sheets/{sheet.id}/decide", {"action": "approve", "comment": "ok"})
    db.refresh(sheet)
    assert sheet.status == "approved"
    assert (sheet.payroll_year, sheet.payroll_month) >= (2026, 8)


def test_withdraw_and_colleague_cannot_withdraw(browser, db, users):
    alice = browser("alice")
    alice.post("/oncall/2026/8", {**_oncall_form([10]), "action": "submit"})
    sheet = db.query(Sheet).filter_by(user_id=users["alice"].id).one()
    browser("carol").post(f"/sheets/{sheet.id}/withdraw")
    db.refresh(sheet)
    assert sheet.status == "submitted"
    alice.post(f"/sheets/{sheet.id}/withdraw")
    db.refresh(sheet)
    assert sheet.status == "draft"


def test_expert_cannot_file_overtime(browser, db, users):
    browser("bob").post("/overtime/entries", _ot("2026-08-10", "18:00", "20:00"))
    assert _overtime_sheets(db, users["bob"]) == []


def test_overtime_split_night_and_weekend_rule(browser, db, users):
    alice = browser("alice")
    # Friday 7 Aug 23:00 -> Sat 02:00 without on-call/pre-approval: Saturday part refused
    alice.post("/overtime/entries", _ot("2026-08-07", "23:00", "02:00"))
    assert _overtime_sheets(db, users["alice"]) == []
    alice.post("/oncall/2026/8", {"oc_2026-08-07": "1", "oc_2026-08-08": "1", "action": "save"})
    alice.post("/overtime/entries", _ot("2026-08-07", "23:00", "02:00", compensation="TIL"))
    sheet = _overtime_sheets(db, users["alice"])[0]
    entries = sorted(sheet.overtime_entries, key=lambda e: e.date)
    assert [(e.day_type, float(e.hours), float(e.night_hours)) for e in entries] == [("W", 1.0, 1.0), ("SAT", 2.0, 2.0)]
    assert [float(e.til_credit) for e in entries] == [1.8, 3.6]
    # overlap refused
    alice.post("/overtime/entries", _ot("2026-08-07", "23:30", "00:00"))
    assert len(_overtime_sheets(db, users["alice"])[0].overtime_entries) == 2


def test_overtime_15_minute_steps_and_typed_times(browser, db, users):
    alice = browser("alice")
    alice.post("/overtime/entries", _ot("2026-08-10", "18:10", "19:00"))   # not a multiple of 15 minutes
    alice.post("/overtime/entries", _ot("2026-08-10", "18:00", "18:20"))
    assert _overtime_sheets(db, users["alice"]) == []
    alice.post("/overtime/entries", _ot("2026-08-10", "18:00", "18:15"))   # 15 minutes is the minimum
    alice.post("/overtime/entries", _ot("2026-08-10", "8.45", "9h30"))     # typed in other notations
    alice.post("/overtime/entries", _ot("2026-08-10", "1000", "11"))
    sheet = _overtime_sheets(db, users["alice"])[0]
    assert sorted(float(e.hours) for e in sheet.overtime_entries) == [0.25, 0.75, 1.0]
    assert sorted(e.start_minute for e in sheet.overtime_entries) == [8 * 60 + 45, 10 * 60, 18 * 60]


def test_overtime_not_in_future(browser, db, users):
    tomorrow = dt.date.today() + dt.timedelta(days=1)
    browser("alice").post("/overtime/entries", _ot(tomorrow.isoformat(), "18:00", "19:00", preapproved="1"))
    assert _overtime_sheets(db, users["alice"]) == []


def test_late_claim_creates_supplementary_sheet(browser, db, users):
    alice, mgr = browser("alice"), browser("manager")
    alice.post("/overtime/entries", _ot("2026-08-10", "18:00", "20:00"))
    first = _overtime_sheets(db, users["alice"])[0]
    alice.post(f"/submit/{first.year}/{first.month}")
    # while submitted, adding is refused
    alice.post("/overtime/entries", _ot("2026-08-11", "18:00", "19:00"))
    assert len(_overtime_sheets(db, users["alice"])[0].overtime_entries) == 1
    mgr.post(f"/sheets/{first.id}/decide", {"action": "approve"})

    # forgotten entry for the already approved month -> supplementary sheet #2
    alice.post("/overtime/entries", _ot("2026-08-11", "18:00", "19:00"))
    sheets = _overtime_sheets(db, users["alice"])
    assert [(s.seq, s.status, len(s.overtime_entries)) for s in sheets] == [(1, "approved", 1), (2, "draft", 1)]
    alice.post(f"/submit/{sheets[1].year}/{sheets[1].month}")
    mgr.post(f"/sheets/{sheets[1].id}/decide", {"action": "approve"})
    supp = _overtime_sheets(db, users["alice"])[1]
    assert supp.status == "approved" and supp.is_late  # approved months after August's cut-off

    # it is exported in its payroll period, flagged as late claim
    r = browser("admin").get(f"/payroll/export/control?year={supp.payroll_year}&month={supp.payroll_month}")
    rows = [row for row in load_workbook(io.BytesIO(r.content))["Overtime Payroll"].iter_rows(min_row=2, values_only=True)
            if row[2] == "Alice"]
    assert any(row[4] == "2026-08" and "Late claim" in row[5] and row[10] == 1.0 for row in rows)
    assert mgr.get(f"/overtime/2026/8?user_id={users['alice'].id}").text.count("Supplementary sheet #2") == 1


def test_export_contains_only_approved_scope(browser, db, users):
    alice, carol = browser("alice"), browser("carol")
    alice.post("/oncall/2026/8", {**_oncall_form([10, 11]), "action": "submit"})
    carol.post("/oncall/2026/8", {**_oncall_form([12]), "action": "submit"})
    alice.post("/overtime/entries", _ot("2026-08-09", "10:00", "13:00", preapproved="1"))
    ot = _overtime_sheets(db, users["alice"])[0]
    alice.post(f"/submit/{ot.year}/{ot.month}")
    mgr = browser("manager")
    for s in db.query(Sheet).filter_by(user_id=users["alice"].id):
        mgr.post(f"/sheets/{s.id}/decide", {"action": "approve"})
    db.expire_all()
    period = db.get(Sheet, ot.id)
    year, month = period.payroll_year, period.payroll_month

    r = browser("admin").get(f"/payroll/export/control?year={year}&month={month}")
    assert r.status_code == 200
    wb = load_workbook(io.BytesIO(r.content))
    oncall_names = [row[2] for row in wb["On-Call"].iter_rows(min_row=2, values_only=True) if row[2]]
    assert oncall_names == ["Alice"]  # carol not approved yet
    payroll = [row for row in wb["Overtime Payroll"].iter_rows(min_row=2, values_only=True) if row[2] == "Alice"][0]
    assert payroll[8] == 3.0 and payroll[12] == 3.0 and payroll[15] == 3.0  # Sun hours, HL100, HL130

    assert browser("alice").get(f"/payroll/export/control?year={year}&month={month}").status_code == 404
    assert mgr.get(f"/payroll/export/control?year={year}&month={month}").status_code == 404   # exports belong to IT Procurement / admin
    head_wb = load_workbook(io.BytesIO(browser("admin").get("/payroll/export/control?year=2026&month=8").content))
    summary_names = {row[2] for row in head_wb["Summary"].iter_rows(min_row=7, values_only=True)}
    assert {"Dave", "Alice", "Carol"} <= summary_names


def test_ldap_sync_creates_manager_link(db, users):
    p = LdapProfile(dn="CN=Eve,OU=Users,DC=corp", username="eve", first_name="Eve", last_name="L",
                    manager=LdapProfile(dn="CN=Boss,OU=Users,DC=corp", username="boss", first_name="Boss"),
                    is_expert=True)
    eve = sync_user(db, p)
    db.commit()
    assert eve.auth_source == "ldap" and eve.is_expert
    assert eve.manager.username == "boss" and eve.manager.ldap_dn == "CN=Boss,OU=Users,DC=corp"
    eve.manager_locked, eve.manager_id = True, users["manager"].id
    sync_user(db, p)
    assert eve.manager_id == users["manager"].id


def test_admin_pages_protected(browser, users):
    assert browser("alice").get("/admin/users").status_code == 404
    admin = browser("admin")
    assert admin.get("/admin/users").status_code == 200
    r = admin.post("/admin/users/save", {"username": "newbie", "password": "short"})
    assert r.headers["location"] == "/admin/users/new"


def test_manager_cycle_prevented(browser, db, users):
    admin = browser("admin")
    head = users["head"]
    admin.post("/admin/users/save", {"user_id": head.id, "manager_id": users["alice"].id, "is_active": "1"})
    db.expire_all()
    assert db.get(User, head.id).manager_id is None
