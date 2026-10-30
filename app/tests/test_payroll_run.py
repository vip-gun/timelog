"""IT Procurement role: payroll reports, the "sent to Finance" record, late payments and notices."""
import datetime as dt
import io

import pytest
from openpyxl import load_workbook
from sqlalchemy import create_engine, text

from conftest import PASSWORD
from timelog import calc
from timelog.db import ensure_columns
from timelog.models import Notification, PayrollRun, Sheet, User
from timelog.security import hash_password

from test_v1_features import _ot

TODAY = dt.date.today()
THIS = (TODAY.year, TODAY.month)
NEXT = (TODAY.year + (TODAY.month == 12), TODAY.month % 12 + 1)


def _procurement(db):
    u = User(username="proc", auth_source="local", first_name="Pat", last_name="Procure", is_procurement=True,
             password_hash=hash_password(PASSWORD), email="proc@example.test")
    db.add(u)
    db.commit()
    return u


def _declare(browser, name):
    """The user's on-call day today + overtime today (this work month), submitted."""
    b = browser(name)
    b.post(f"/oncall/{TODAY.year}/{TODAY.month}", {f"oc_{TODAY.isoformat()}": "1", "action": "save"})
    if name != "bob":
        b.post("/overtime/entries", _ot(TODAY.isoformat(), "18:00", "19:30", preapproved="1"))
    b.post(f"/submit/{TODAY.year}/{TODAY.month}")


def _approve_all(browser, db, who="manager"):
    db.expire_all()
    for s in db.query(Sheet).filter(Sheet.status == "submitted"):
        browser(who).post(f"/sheets/{s.id}/decide", {"action": "approve"})
    db.expire_all()


def _download_both(proc, year, month):
    for kind in ("oncall", "overtime"):
        assert proc.get(f"/payroll/export/{kind}?year={year}&month={month}").status_code == 200


# ---------------------------------------------------------------- pure rules
def test_closed_period_pushes_approval_to_next_month():
    # approved on time -> its own month; once that period is sent it moves on, even before the cut-off date
    assert calc.payroll_period(2026, 8, dt.date(2026, 9, 3), 5) == (2026, 8)
    assert calc.payroll_period(2026, 8, dt.date(2026, 9, 3), 5, closed={(2026, 8)}) == (2026, 9)
    assert calc.payroll_period(2026, 8, dt.date(2026, 9, 6), 5) == (2026, 9)           # after the 5th
    assert calc.payroll_period(2026, 12, dt.date(2027, 1, 4), 5, closed={(2026, 12)}) == (2027, 1)
    assert calc.payroll_period(2026, 8, dt.date(2026, 9, 3), 5, closed={(2026, 8), (2026, 9)}) == (2026, 10)


def test_additive_schema_upgrade_adds_the_role_column(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'old.db'}")
    with engine.begin() as conn:
        conn.execute(text("CREATE TABLE users (id INTEGER PRIMARY KEY, username VARCHAR(128), is_admin BOOLEAN)"))
        conn.execute(text("INSERT INTO users (username, is_admin) VALUES ('old', 1)"))
    added = ensure_columns(engine)
    assert "users.is_procurement" in added
    with engine.connect() as conn:
        assert conn.execute(text("SELECT is_procurement FROM users")).scalar() in (0, False)
    assert ensure_columns(engine) == []                                              # idempotent


# ---------------------------------------------------------------- the role
def test_procurement_role_is_scoped(browser, db, users):
    _procurement(db)
    proc, alice, mgr = browser("proc"), browser("alice"), browser("manager")
    assert proc.get("/payroll").status_code == 200 and "Payroll" in proc.get("/").text
    assert alice.get("/payroll").status_code == 404 and mgr.get("/payroll").status_code == 404
    assert browser("admin").get("/payroll").status_code == 200
    # no access to the individual sheets of employees
    assert proc.get(f"/oncall/{TODAY.year}/{TODAY.month}?user_id={users['alice'].id}").status_code == 404
    # nobody but IT Procurement / admin records the run
    assert alice.post(f"/payroll/{THIS[0]}/{THIS[1]}/sent", {"confirm": "1"}).status_code == 404


def test_admin_can_assign_the_role(browser, db, users):
    admin = browser("admin")
    r = admin.post("/admin/users/save", {"user_id": str(users["dave"].id), "username": "dave", "first_name": "Dave",
                                         "last_name": "Test", "is_procurement": "1", "is_active": "1"})
    db.expire_all()
    assert db.get(User, users["dave"].id).is_procurement is True


# ---------------------------------------------------------------- the workflow
def test_download_record_close_and_late_payment(browser, db, users):
    _procurement(db)
    proc = browser("proc")
    _declare(browser, "alice")
    _approve_all(browser, db)
    alice_sheets = db.query(Sheet).filter_by(user_id=users["alice"].id).all()
    assert {(s.payroll_year, s.payroll_month) for s in alice_sheets} == {THIS} and not any(s.is_late for s in alice_sheets)

    page = proc.get(f"/payroll?year={THIS[0]}&month={THIS[1]}").text
    assert "Alice" in page and "not downloaded yet" in page and "disabled" in page

    # the record needs both reports downloaded first, and a ticked confirmation
    proc.post(f"/payroll/{THIS[0]}/{THIS[1]}/sent", {"confirm": "1"})
    assert db.query(PayrollRun).count() == 0
    _download_both(proc, *THIS)
    proc.post(f"/payroll/{THIS[0]}/{THIS[1]}/sent", {})
    assert db.query(PayrollRun).count() == 0

    # something approved after the download invalidates it: the reports no longer match what would be recorded
    _declare(browser, "carol")
    _approve_all(browser, db)
    proc.post(f"/payroll/{THIS[0]}/{THIS[1]}/sent", {"confirm": "1"})
    assert db.query(PayrollRun).count() == 0 and "approved since" in proc.get(f"/payroll?year={THIS[0]}&month={THIS[1]}").text

    _download_both(proc, *THIS)
    proc.post(f"/payroll/{THIS[0]}/{THIS[1]}/sent", {"confirm": "1", "note": "mail to finance 5/11"})
    db.expire_all()
    run = db.query(PayrollRun).one()
    assert (run.year, run.month, run.employees, run.oncall_sheets, run.overtime_sheets) == (*THIS, 2, 2, 2)
    assert run.note == "mail to finance 5/11" and run.sent_by.username == "proc"
    sheet = db.get(Sheet, alice_sheets[0].id)
    assert sheet.sent_at is not None
    assert "Sent to Finance" in browser("alice").get(f"/oncall/{TODAY.year}/{TODAY.month}").text
    sent_notice = db.query(Notification).filter_by(user_id=users["alice"].id).all()
    assert any("sent to Finance" in n.message for n in sent_notice)
    assert db.query(Sheet).filter(Sheet.id == sheet.id).one().status == "approved"
    # closed: a second record is refused, and an approved + sent sheet cannot be reopened any more
    proc.post(f"/payroll/{THIS[0]}/{THIS[1]}/sent", {"confirm": "1"})
    assert db.query(PayrollRun).count() == 1
    r = browser("admin").post(f"/sheets/{sheet.id}/reopen", {"reason": "oops"})
    db.expire_all()
    assert db.get(Sheet, sheet.id).status == "approved"

    # dave declares after the period was sent: approved on time by date, but paid with the following month
    _declare(browser, "dave")
    for s in db.query(Sheet).filter(Sheet.status == "submitted"):
        browser("head").post(f"/sheets/{s.id}/decide", {"action": "approve"})
    db.expire_all()
    late = db.query(Sheet).filter_by(user_id=users["dave"].id).all()
    assert late and all((s.payroll_year, s.payroll_month) == NEXT and s.is_late for s in late)
    notes = [n.message for n in db.query(Notification).filter_by(user_id=users["dave"].id)]
    assert any("after the Finance cut-off" in m and "payroll, not with" in m for m in notes)
    assert "after the Finance cut-off" in browser("dave").get("/").text                 # banner until dismissed
    nid = db.query(Notification).filter_by(user_id=users["dave"].id, kind="late").first().id
    browser("dave").post(f"/notices/{nid}/read", {"back": "/"})
    db.expire_all()
    assert db.get(Notification, nid).read_at is not None
    assert browser("carol").post(f"/notices/{nid}/read", {}).status_code in (200, 303)   # someone else's notice is untouched
    assert db.get(Notification, nid).read_at is not None

    # next period: the expected month plus what was validated late, as per date, one tab per earlier work month
    page = proc.get(f"/payroll?year={NEXT[0]}&month={NEXT[1]}").text
    assert "Dave" in page and "late" in page and "Late (earlier months)" in page
    for kind in ("oncall", "overtime"):
        wb = load_workbook(io.BytesIO(proc.get(f"/payroll/export/{kind}?year={NEXT[0]}&month={NEXT[1]}").content))
        assert any(name.endswith("(late)") for name in wb.sheetnames), wb.sheetnames


def test_late_submission_is_flagged_to_the_user(browser, db, users):
    _procurement(db)
    proc, alice = browser("proc"), browser("alice")
    _declare(browser, "alice")
    _approve_all(browser, db)
    _download_both(proc, *THIS)
    proc.post(f"/payroll/{THIS[0]}/{THIS[1]}/sent", {"confirm": "1"})
    # more overtime for the closed month: told at once that it will be paid next month
    alice.post("/overtime/entries", _ot(TODAY.isoformat(), "20:00", "21:00", preapproved="1"))
    page = alice.get("/overtime").text
    assert "paid with the" in page
    alice.post(f"/submit/{TODAY.year}/{TODAY.month}")
    db.expire_all()
    msgs = [n.message for n in db.query(Notification).filter_by(user_id=users["alice"].id, kind="late")]
    assert any("cut-off" in m and "will be paid with the" in m for m in msgs)


def test_on_time_submission_gets_the_deadline(browser, db, users):
    alice = browser("alice")
    alice.post(f"/oncall/{TODAY.year}/{TODAY.month}", {f"oc_{TODAY.isoformat()}": "1", "action": "submit"})
    ny, nm = NEXT
    assert f"approve it by 05/{nm:02d}/{ny}" in alice.get(f"/oncall/{TODAY.year}/{TODAY.month}").text


def test_payroll_cutoff_is_a_setting(browser, db, users):
    admin = browser("admin")
    admin.post("/admin/settings", {"show_amounts": "1", "til_enabled": "1", "payroll_cutoff": "7"})
    assert 'value="7"' in admin.get("/admin/settings").text
    admin.post("/admin/settings", {"payroll_cutoff": "40"})                      # invalid: ignored
    assert 'value="7"' in admin.get("/admin/settings").text


def test_navigation_order_and_statistics_page(browser, db, users):
    _procurement(db)
    mgr, admin, proc = browser("manager"), browser("admin"), browser("proc")

    def nav(html):
        header = html.split("</header>")[0]
        links = [t.strip() for t in __import__("re").findall(r'<a[^>]*>([^<]+)</a>', header.split('<nav>')[1].split("</nav>")[0])]
        return links, header

    links, header = nav(admin.get("/").text)
    assert links == ["Dashboard", "On-call", "Overtime", "Approvals", "Statistics", "Payroll"]
    # Admin sits on the right, between the navigation and the user block
    assert header.index("</nav>") < header.index('class="admin-link') < header.index('class="who"')
    assert nav(proc.get("/").text)[0] == ["Dashboard", "On-call", "Overtime", "Payroll"] and 'class="admin-link' not in nav(proc.get("/").text)[1]
    assert nav(mgr.get("/").text)[0] == ["Dashboard", "On-call", "Overtime", "Approvals", "Statistics"]

    page = mgr.get("/team").text
    assert "<h1>Statistics</h1>" in page and "<title>Timelog - Statistics" in page
    assert "/team/export" not in page and "/payroll/export" not in page
    assert proc.get("/team").status_code == 404           # statistics are for managers and admins


def test_admin_menu_order(browser, users):
    html = browser("admin").get("/admin/settings").text
    menu = html.split('<nav class="subnav">')[1].split("</nav>")[0]
    labels = [t.strip().replace("&amp;", "&") for t in __import__("re").findall(r"<a[^>]*>([^<]+)</a>", menu)]
    assert labels == ["Documentation", "Settings", "Users", "Grades & on-call rates", "Legal holidays", "TIL factors", "Backup", "Audit log"]


def test_skip_level_approval(browser, db, users):
    """head -> manager -> alice: the skip-level manager (head) may approve alice's sheet; peers and others may not."""
    from timelog.models import AuditLog
    alice, head, mgr, dave = browser("alice"), browser("head"), browser("manager"), browser("dave")
    _declare(browser, "alice")
    db.expire_all()
    sheets = db.query(Sheet).filter_by(user_id=users["alice"].id, status="submitted").all()
    assert len(sheets) == 2

    # shown in the skip-level manager's approvals list and counted on the dashboard
    assert "Alice" in head.get("/approvals").text and "Alice" in mgr.get("/approvals").text
    assert "Alice" not in dave.get("/approvals").text                       # dave reports to head, not to alice's chain
    assert head.get(f"/oncall/{TODAY.year}/{TODAY.month}?user_id={users['alice'].id}").text.count("Approve") >= 1

    # not allowed: the employee herself and someone outside her chain (dave: a peer of her manager's branch)
    alice.post(f"/sheets/{sheets[0].id}/decide", {"action": "approve"})
    dave.post(f"/sheets/{sheets[0].id}/decide", {"action": "approve"})
    db.expire_all()
    assert db.get(Sheet, sheets[0].id).status == "submitted"

    # allowed: head, two levels above, approves one sheet; the direct manager the other
    head.post(f"/sheets/{sheets[0].id}/decide", {"action": "approve"})
    mgr.post(f"/sheets/{sheets[1].id}/decide", {"action": "approve"})
    db.expire_all()
    first, second = db.get(Sheet, sheets[0].id), db.get(Sheet, sheets[1].id)
    assert first.status == second.status == "approved" and first.decided_by.username == "head"
    flags = {a.actor.username: ('"skip_level": true' in a.details) for a in db.query(AuditLog).filter_by(action="approve")}
    assert flags == {"head": True, "manager": False}                         # the audit log tells skip-level from direct approvals
