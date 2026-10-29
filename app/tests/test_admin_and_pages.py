"""Admin CRUD, approvals/team pages and login edge cases."""
import datetime as dt

from timelog.config import get_settings
from timelog.models import Grade, Holiday, OvertimeRule, Sheet, User


def _submit_oncall(browser, db, users, who="alice", day="2026-08-10"):
    b = browser(who)
    b.post("/oncall/2026/8", {f"oc_{day}": "1", "action": "submit"})
    db.expire_all()
    return b, db.query(Sheet).filter_by(user_id=users[who].id, kind="oncall").one()


# ---------------------------------------------------------------- admin pages
def test_admin_pages_render(browser, users):
    admin = browser("admin")
    for url in ("/admin/users", "/admin/users?q=ali", "/admin/users/new", f"/admin/users/{users['alice'].id}",
                "/admin/grades", "/admin/holidays", "/admin/rules", "/admin/audit", "/admin/settings"):
        assert admin.get(url).status_code == 200, url
    assert admin.get("/admin/users/9999").status_code == 404


def test_admin_user_create_update_and_errors(browser, db, users):
    admin = browser("admin")
    form = {"username": "Newbie", "first_name": "New", "last_name": "Bie", "team": "QA", "is_active": "1",
            "password": "long-enough-pw1", "is_expert": "1", "grade_id": "1", "manager_id": str(users["manager"].id)}
    assert admin.post("/admin/users/save", form).headers["location"] == "/admin/users"
    db.expire_all()
    new = db.query(User).filter_by(username="newbie").one()
    assert new.is_expert and new.manager_id == users["manager"].id and new.password_hash
    assert browser().login("newbie", "long-enough-pw1").status_code == 303  # signs in with the chosen password

    admin.post("/admin/users/save", form)  # duplicate username -> flash, no crash
    admin.post("/admin/users/save", {"username": "", "password": "x"})  # missing username
    admin.post("/admin/users/save", {"user_id": new.id, "is_active": "1", "password": "changed-pw-123"})
    assert browser().login("newbie", "changed-pw-123").status_code == 303
    # an administrator cannot demote or deactivate themself
    me = db.query(User).filter_by(username="admin").one()
    admin.post("/admin/users/save", {"user_id": me.id})
    db.expire_all()
    assert db.get(User, me.id).is_admin and db.get(User, me.id).is_active


def test_admin_grades_holidays_rules_audit(browser, db, users):
    admin = browser("admin")
    admin.post("/admin/grades", {"code": "sr", "name": "Senior", "rate_weekday": "60", "rate_saturday": "80",
                                 "rate_sunday": "80,5", "rate_holiday": "100", "is_default": "1"})
    db.expire_all()
    senior = db.query(Grade).filter_by(code="SR").one()
    assert senior.is_default and float(senior.rate_sunday) == 80.5
    admin.post("/admin/grades", {"grade_id": senior.id, "code": "SR", "name": "Senior 2", "rate_weekday": "61",
                                 "rate_saturday": "1", "rate_sunday": "1", "rate_holiday": "1"})
    admin.post("/admin/grades", {"code": "BAD", "rate_weekday": "abc"})  # invalid number
    admin.post("/admin/grades", {"code": "SR", "rate_weekday": "1", "rate_saturday": "1",
                                 "rate_sunday": "1", "rate_holiday": "1"})  # duplicate code
    db.expire_all()
    assert db.query(Grade).filter_by(code="BAD").count() == 0

    admin.post("/admin/holidays", {"date": "2030-05-01", "name": "Labour Day"})
    admin.post("/admin/holidays", {"date": "2030-05-01", "name": "dup"})
    admin.post("/admin/holidays", {"date": "not-a-date", "name": "x"})
    db.expire_all()
    h = db.query(Holiday).filter(Holiday.date == dt.date(2030, 5, 1)).one()
    admin.post(f"/admin/holidays/{h.id}/delete")
    db.expire_all()
    assert db.query(Holiday).filter(Holiday.date == dt.date(2030, 5, 1)).count() == 0

    rules = {f"{p}_{t}": "2" for t in ("W", "SAT", "SUN", "H") for p in ("day", "night")}
    admin.post("/admin/rules", rules)
    admin.post("/admin/rules", {**rules, "day_W": "oops"})
    db.expire_all()
    assert float(db.get(OvertimeRule, "W").til_factor) == 2.0
    page = admin.get("/admin/audit").text
    assert "grade_save" in page and "holiday_add" in page and "rules_update" in page


# ---------------------------------------------------------------- approvals / team / dashboard
def test_approvals_team_and_reopen(browser, db, users):
    alice, sheet = _submit_oncall(browser, db, users)
    mgr, admin = browser("manager"), browser("admin")
    assert "Alice" in mgr.get("/approvals").text
    assert mgr.get(f"/sheets/{sheet.id}", follow_redirects=False).status_code == 303  # to the sheet page
    assert browser("carol").get(f"/sheets/{sheet.id}").status_code == 404
    mgr.post(f"/sheets/{sheet.id}/decide", {"action": "approve"})
    assert "approved" in mgr.get("/approvals").text.lower()

    for url in ("/team", "/team?year=2026&month=8", "/team?year=2026&month=8&team=Production"):
        assert mgr.get(url).status_code == 200
    assert browser("head").get("/team?year=2026&month=8").status_code == 200
    assert browser("alice").get("/team").status_code == 404

    # only admins reopen, with a reason
    mgr.post(f"/sheets/{sheet.id}/reopen", {"reason": "oops"})
    admin.post(f"/sheets/{sheet.id}/reopen", {"reason": ""})
    db.expire_all()
    assert db.get(Sheet, sheet.id).status == "approved"
    admin.post(f"/sheets/{sheet.id}/reopen", {"reason": "correction"})
    db.expire_all()
    assert db.get(Sheet, sheet.id).status == "draft"


def test_overtime_delete_and_redirects(browser, db, users):
    alice, carol = browser("alice"), browser("carol")
    alice.post("/overtime/entries", {"date": "2026-08-10", "start": "18:00", "end": "19:00", "description": "x"})
    entry = db.query(Sheet).filter_by(user_id=users["alice"].id, kind="overtime").one().overtime_entries[0]
    carol.post(f"/overtime/entries/{entry.id}/delete")  # someone else's entry: refused
    db.expire_all()
    assert db.query(Sheet).filter_by(user_id=users["alice"].id).one().overtime_entries
    alice.post(f"/overtime/entries/{entry.id}/delete")
    db.expire_all()
    assert not db.query(Sheet).filter_by(user_id=users["alice"].id).one().overtime_entries
    assert alice.get("/overtime", follow_redirects=False).status_code == 303
    assert alice.get("/oncall", follow_redirects=False).status_code == 303
    assert alice.get("/overtime/1999/1").status_code == 404
    alice.post("/overtime/entries", {"date": "bad", "start": "18:00", "end": "19:00", "description": "x"})


def test_dashboard_with_data(browser, db, users):
    alice, _ = _submit_oncall(browser, db, users)
    page = alice.get("/").text
    assert "On-call days this month" in page
    assert browser().get("/healthz").status_code == 200


# ---------------------------------------------------------------- login edge cases
def test_login_throttle_inactive_and_logout(browser, db, users):
    b = browser()
    for _ in range(5):
        assert b.login("dave", "wrong-password").status_code == 401
    assert b.login("dave", "wrong-password").status_code == 429

    users["carol"].is_active = False
    db.commit()
    assert browser().login("carol").status_code == 401

    alice = browser("alice")
    assert alice.get("/login", follow_redirects=False).status_code == 303  # already signed in
    assert alice.post("/logout").headers["location"] == "/login"
    assert alice.get("/", follow_redirects=False).status_code == 303


def test_login_next_is_local_only_and_admin_only_mode(browser, users):
    b = browser()
    page = b.get("/login").text
    from conftest import PASSWORD, csrf_of
    r = b.client.post("/login", data={"username": "alice", "password": PASSWORD, "csrf_token": csrf_of(page),
                                      "next": "//evil.example"}, follow_redirects=False)
    assert r.headers["location"] == "/"
    get_settings().local_login_admin_only = True
    assert browser().login("alice").status_code == 401
    assert browser().login("admin").status_code == 303
