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
