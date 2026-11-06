"""Team management, bulk user edit, on-call per team, guide adaptation and the export's payroll-check block."""
import io

from openpyxl import load_workbook

from timelog.models import AuditLog, Team, User

from test_finance_export import _approved_august


def _admin(browser):
    b = browser()
    assert b.login("admin").status_code == 303
    return b


def test_team_oncall_switch_hides_oncall_for_members(browser, db, users):
    admin = _admin(browser)
    admin.post("/admin/users/bulk", {"user_ids": [str(users["bob"].id)], "is_expert": "0"})   # bob was the only Expert
    r = admin.post("/admin/teams/save", {"name": "Production", "new_name": "Production"})     # on-call unticked
    db.expire_all()
    team = db.query(Team).one()
    assert team.oncall_enabled is False
    assert db.query(AuditLog).filter_by(action="team_update").count() == 1

    alice = browser("alice")
    assert 'href="/oncall"' not in alice.get("/").text
    r = alice.get("/oncall/2026/8", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/"
    r = alice.post("/oncall/2026/8", {"oc_2026-08-10": "1"})
    assert r.status_code == 303 and r.headers["location"] == "/"
    guide = alice.get("/guide").text
    assert "does overtime only" in guide and "Declaring on-call" not in guide and "Declaring overtime" in guide
    # overtime still works
    assert alice.get("/overtime/2026/8").status_code == 200


def test_cannot_switch_off_team_with_experts_or_move_expert_in(browser, db, users):
    admin = _admin(browser)
    admin.post("/admin/teams/save", {"name": "Production", "new_name": "Production"})
    db.expire_all()
    assert db.query(Team).count() == 0                        # refused: bob is an Expert
    admin.post("/admin/teams/create", {"name": "Helpdesk"})   # created with on-call off
    r = admin.post("/admin/users/bulk", {"user_ids": [str(users["bob"].id), str(users["alice"].id)], "team": "Helpdesk"})
    db.expire_all()
    assert db.get(User, users["bob"].id).team == "Production"          # Expert skipped
    assert db.get(User, users["alice"].id).team == "Helpdesk"


def test_rename_and_delete_team(browser, db, users):
    admin = _admin(browser)
    admin.post("/admin/teams/save", {"name": "Production", "new_name": "Operations", "oncall": "1"})
    db.expire_all()
    assert {u.team for u in db.query(User).filter(User.username != "admin")} == {"Operations"}
    admin.post("/admin/teams/delete", {"name": "Operations"})           # members: refused
    db.expire_all()
    assert db.query(Team).filter_by(name="Operations").count() == 1
    assert "Operations" in admin.get("/admin/teams").text


def test_bulk_edit_manager_team_expert(browser, db, users):
    admin = _admin(browser)
    ids = [str(users["alice"].id), str(users["carol"].id)]
    r = admin.post("/admin/users/bulk", {"user_ids": ids, "manager_id": str(users["dave"].id), "team": "Data", "is_expert": "1"})
    assert r.status_code == 303
    db.expire_all()
    for name in ("alice", "carol"):
        u = db.get(User, users[name].id)
        assert (u.manager_id, u.team, u.is_expert) == (users["dave"].id, "Data", True)
    assert db.query(AuditLog).filter_by(action="users_bulk_update").count() == 1
    # head cannot be put below dave (dave reports to head): cycle skipped, nothing else changes
    r = admin.post("/admin/users/bulk", {"user_ids": [str(users["head"].id)], "manager_id": str(users["dave"].id)})
    db.expire_all()
    assert db.get(User, users["head"].id).manager_id is None
    r = admin.post("/admin/users/bulk", {"user_ids": ids, "manager_id": "__none__"})
    db.expire_all()
    assert db.get(User, users["alice"].id).manager_id is None
    # nothing selected / nothing to change
    assert admin.post("/admin/users/bulk", {"manager_id": "__none__"}).status_code == 303
    assert admin.post("/admin/users/bulk", {"user_ids": ids}).status_code == 303


def test_bulk_edit_is_admin_only(browser, users):
    r = browser("manager").post("/admin/users/bulk", {"user_ids": ["1"], "team": "X"})
    assert r.status_code in (302, 303, 403, 404)
    assert "login" in r.headers.get("location", "login") or r.status_code in (403, 404)


def test_ldap_team_lock(db, users):
    from timelog.ldap_auth import LdapProfile, _upsert
    u = db.get(User, users["alice"].id)
    u.auth_source, u.ldap_dn, u.team_locked, u.team = "ldap", "cn=alice", True, "Manual"
    db.commit()
    _upsert(db, LdapProfile(dn="cn=alice", username="alice", team="FromAD"))
    assert u.team == "Manual"
    u.team_locked = False
    _upsert(db, LdapProfile(dn="cn=alice", username="alice", team="FromAD"))
    assert u.team == "FromAD"


def test_guide_has_manager_section(browser, users):
    assert "For managers" in browser("manager").get("/guide").text
    assert "For managers" not in browser("alice").get("/guide").text


def test_oncall_export_has_payroll_check_block(browser, db, users):
    mgr, py, pm = _approved_august(browser, db, users)
    r = browser("admin").get(f"/payroll/export/oncall?year={py}&month={pm}")
    ws = load_workbook(io.BytesIO(r.content))["AUG (late)"]
    title = next(r for r in range(6, 120) if ws.cell(r, 4).value and "PRICING SHEET" in str(ws.cell(r, 4).value))
    assert "ON CALL PRIME request" in ws.cell(title, 4).value
    assert ws.cell(title + 2, 37).value.month == 8                       # AK: the month label
    assert (ws.cell(title + 2, 38).value, ws.cell(title + 3, 38).value) == ("Addition for Payroll's", "Check")
    names = {ws.cell(r, 37).value: r for r in range(title + 7, title + 60) if ws.cell(r, 37).value}
    alice = next(r for n, r in names.items() if n.startswith("Alice"))
    assert ws.cell(alice, 38).value == ws.cell(alice, 35).value == 135.0   # AL = AI: 45 (Mon) + 90 (15 Aug holiday)
    # team subtotal on the blank row closing the team, same value in AI of the team header; grand total at the end
    gap = next(r for r in range(alice, alice + 20) if ws.cell(r, 2).value is None)
    total = sum(ws.cell(r, 38).value or 0 for r in range(title + 7, gap))
    assert ws.cell(gap, 39).value == total == ws.cell(next(r for r in range(gap, title, -1) if ws.cell(r, 2).value == "Production"), 35).value
    assert ws.cell(gap + 1, 38).value == total


def test_reviewer_sees_preapproved_entries(browser, db, users):
    from test_v1_features import _ot
    alice, mgr = browser("alice"), browser("manager")
    alice.post("/overtime/entries", _ot("2026-08-09", "10:00", "12:30", preapproved="1", description="Planned release"))
    alice.post("/submit/2026/8")
    page = mgr.get(f"/overtime/2026/8?user_id={users['alice'].id}").text
    assert 'tag tag-preapproved' in page and "preapproved-note" not in page          # Context column tag only, no banner
    assert "1 pre-approved" in mgr.get("/approvals").text
    assert 'class="avatar"' not in page
