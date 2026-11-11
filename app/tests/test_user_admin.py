from timelog.models import AuditLog, Sheet, User
from sqlalchemy import select
from timelog import workflow
import datetime as dt


def test_team_suggestions_on_user_form(browser, users):
    admin = browser("admin")
    html = admin.get("/admin/users/new").text
    assert 'list="team-list"' in html and '<option value="Production">' in html
    assert '<option value="Production">' in admin.get(f"/admin/users/{users['alice'].id}").text


def test_delete_user_removes_unapproved_data(browser, db, users):
    admin = browser("admin")
    alice = users["alice"]
    alice_id = alice.id
    sheet = workflow.get_or_create_sheet(db, alice, "oncall", 2026, 9)
    workflow.save_oncall(db, sheet, [workflow.OnCallInput(dt.date(2026, 9, 4))])
    db.commit()
    page = admin.get(f"/admin/users/{alice_id}").text
    assert f"/admin/users/{alice_id}/delete" in page
    r = admin.post(f"/admin/users/{alice_id}/delete")
    assert r.status_code == 303
    db.expire_all()
    assert db.scalar(select(User).where(User.username == "alice")) is None
    assert db.scalar(select(Sheet).where(Sheet.user_id == alice_id)) is None
    assert any(a.action == "user_delete" for a in db.scalars(select(AuditLog)))


def test_cannot_delete_self_or_user_with_approved_sheet(browser, db, users):
    admin = browser("admin")
    me = db.scalar(select(User).where(User.username == "admin"))
    admin.post(f"/admin/users/{me.id}/delete")
    assert db.scalar(select(User).where(User.username == "admin")) is not None
    sheet = workflow.get_or_create_sheet(db, users["carol"], "oncall", 2026, 9)
    sheet.status = "approved"
    db.commit()
    admin.post(f"/admin/users/{users['carol'].id}/delete")
    db.expire_all()
    assert db.scalar(select(User).where(User.username == "carol")) is not None


def test_delete_requires_admin_and_unlinks_reports(browser, db, users):
    assert browser("alice").post(f"/admin/users/{users['bob'].id}/delete").status_code in (403, 404)
    admin = browser("admin")
    admin.post(f"/admin/users/{users['manager'].id}/delete")
    db.expire_all()
    assert db.get(User, users["alice"].id).manager_id is None


def test_archive_and_restore_keeps_records(browser, db, users):
    admin = browser("admin")
    carol_id = users["carol"].id
    sheet = workflow.get_or_create_sheet(db, users["carol"], "oncall", 2026, 9)
    sheet.status = "approved"
    db.commit()
    assert admin.post(f"/admin/users/{carol_id}/archive").status_code == 303
    db.expire_all()
    carol = db.get(User, carol_id)
    assert carol.is_archived and not carol.is_active
    assert db.scalar(select(Sheet).where(Sheet.user_id == carol_id)) is not None
    assert f"/admin/users/{carol_id}\"" not in admin.get("/admin/users").text
    assert f"/admin/users/{carol_id}\"" in admin.get("/admin/users?archived=1").text
    assert browser().login("carol").status_code != 303
    admin.post(f"/admin/users/{carol_id}/unarchive")
    db.expire_all()
    assert db.get(User, carol_id).is_active and not db.get(User, carol_id).is_archived
    assert {"user_archive", "user_unarchive"} <= {a.action for a in db.scalars(select(AuditLog))}


def test_cannot_archive_self(browser, db, users):
    admin = browser("admin")
    me = db.scalar(select(User).where(User.username == "admin"))
    admin.post(f"/admin/users/{me.id}/archive")
    db.expire_all()
    assert db.get(User, me.id).is_active


def test_edit_form_offers_archive_and_delete(browser, users):
    admin = browser("admin")
    html = admin.get(f"/admin/users/{users['carol'].id}").text
    assert html.lstrip().startswith("<!doctype html>")
    assert f"/admin/users/{users['carol'].id}/archive" in html and f"/admin/users/{users['carol'].id}/delete" in html
    assert html.index("</form>") < html.index("Archive or delete")


def test_admin_deletes_submissions(browser, db, users):
    from timelog.models import OnCallDay
    admin = browser("admin")
    sheet = workflow.get_or_create_sheet(db, users["carol"], "oncall", 2026, 9)
    workflow.save_oncall(db, sheet, [workflow.OnCallInput(dt.date(2026, 9, 4))])
    sheet.status = "approved"
    db.commit()
    sid = sheet.id
    page = admin.get("/admin/sheets").text
    assert f'value="{sid}"' in page
    assert browser("alice").get("/admin/sheets").status_code in (403, 404)
    admin.post("/admin/sheets/delete", {"sheet_ids": str(sid), "confirm": "nope"})
    db.expire_all()
    assert db.get(Sheet, sid) is not None
    assert admin.post("/admin/sheets/delete", {"sheet_ids": str(sid), "confirm": "DELETE"}).status_code == 303
    db.expire_all()
    assert db.get(Sheet, sid) is None
    assert db.scalar(select(OnCallDay).where(OnCallDay.sheet_id == sid)) is None
    assert any(a.action == "sheets_delete" for a in db.scalars(select(AuditLog)))


def test_submissions_filters_and_delete_edge_cases(browser, db, users):
    admin = browser("admin")
    s1 = workflow.get_or_create_sheet(db, users["carol"], "oncall", 2026, 9)
    s2 = workflow.get_or_create_sheet(db, users["alice"], "overtime", 2025, 3)
    db.commit()
    both = admin.get("/admin/sheets").text
    assert f'name="sheet_ids" value="{s1.id}"' in both and f'name="sheet_ids" value="{s2.id}"' in both
    page = admin.get(f"/admin/sheets?user_id={users['carol'].id}&status=draft&kind=oncall&year=2026").text
    assert f'name="sheet_ids" value="{s1.id}"' in page and f'name="sheet_ids" value="{s2.id}"' not in page
    assert "No submissions match" in admin.get("/admin/sheets?status=approved").text
    # nothing ticked, unknown id and a foreign redirect target are all harmless
    assert admin.post("/admin/sheets/delete", {"confirm": "DELETE"}).status_code == 303
    assert admin.post("/admin/sheets/delete", {"sheet_ids": ["²", "9" * 30], "confirm": "DELETE"}).status_code == 303
    r = admin.post("/admin/sheets/delete", {"sheet_ids": "999999", "confirm": "DELETE", "back": "https://evil.example"})
    assert r.status_code == 303 and r.headers["location"].startswith("/admin/sheets")
    db.expire_all()
    assert db.get(Sheet, s1.id) is not None and db.get(Sheet, s2.id) is not None


def test_notification_bell(browser, db, users):
    alice, mgr = browser("alice"), browser("manager")
    assert 'class="bell"' in alice.get("/").text and "Nothing new" in alice.get("/").text
    sheet = workflow.get_or_create_sheet(db, users["alice"], "oncall", 2026, 9)
    workflow.save_oncall(db, sheet, [workflow.OnCallInput(dt.date(2026, 9, 4))])
    sheet.status, sheet.submitted_at = "submitted", dt.datetime.utcnow()
    db.commit()
    page = mgr.get("/").text
    assert "waiting for your approval" in page and 'class="bell-count">1<' in page
    sheet.status, sheet.decision_comment, sheet.decided_at = "rejected", "fix day", dt.datetime.utcnow()
    db.commit()
    page = alice.get("/").text
    assert "was rejected: fix day" in page and 'class="bell-count">1<' in page


def test_management_role_approves_only(browser, db, users):
    from timelog.models import User as U
    admin = browser("admin")
    carol = users["carol"]
    admin.post("/admin/users/save", {"user_id": carol.id, "first_name": "Carol", "is_management": "1", "is_active": "1",
                                     "team": carol.team or "", "grade_id": ""})
    db.expire_all()
    assert db.get(U, carol.id).is_management is True
    c = browser("carol")
    home = c.get("/").text
    assert "approval only" in home and 'href="/overtime"' not in home and 'href="/oncall"' not in home
    assert c.get("/oncall/2026/9", follow_redirects=False).status_code == 303
    assert c.get("/overtime/2026/9", follow_redirects=False).status_code == 303
    c.post("/overtime/entries", {"date": "2026-09-03", "start": "18:00", "end": "19:00", "description": "x"})
    c.post("/oncall/2026/9", {"oc_2026-09-04": "1", "action": "save"})
    assert c.post("/submit/2026/9", {}).status_code == 303
    db.expire_all()
    assert db.scalar(select(Sheet).where(Sheet.user_id == carol.id)) is None
    assert "Approvals" in c.get("/guide").text and 'id="submit"' not in c.get("/guide").text
    assert "Management" in admin.get("/admin/users").text
