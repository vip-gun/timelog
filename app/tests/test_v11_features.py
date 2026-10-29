"""v1.1: title case, one submission for on-call + overtime, saved signature picture, 30-minute time dropdowns."""
from timelog.models import SavedSignature, Signature

from test_v1_features import SIG, _ot, _sheets


def test_title_words_capitalised(browser, users):
    page = browser("alice").get("/overtime/2026/8").text
    import re
    title = re.search(r"<title>(.*?)</title>", page).group(1)
    assert title.startswith("Timelog - Overtime "), title
    assert all(w[0].isupper() or w[0].isdigit() for w in title.replace(" - ", " ").split()), title
    assert "Sign Out" in page
    assert "<title>Timelog - Sign In</title>" in browser().get("/login").text


def test_time_dropdowns_in_30_minute_steps(browser, users):
    page = browser("alice").get("/overtime/2026/8").text
    assert '<select name="start" required>' in page and '<select name="end" required>' in page
    assert '<option value="00:30">' in page and '<option value="23:30">' in page and '<option value="24:00">' in page
    assert '<option value="00:15">' not in page and 'type="time"' not in page


def test_one_submission_sends_oncall_and_overtime(browser, db, users):
    alice, mgr = browser("alice"), browser("manager")
    alice.post("/oncall/2026/8", {"oc_2026-08-10": "1", "action": "save"})
    alice.post("/overtime/entries", _ot("2026-08-10", "18:00", "19:00"))
    assert [s.status for s in _sheets(db, users["alice"], "oncall")] == ["draft"]
    page = alice.get("/overtime/2026/8").text
    assert 'action="/submit/2026/8"' in page and "sheets/" not in page.split("submit-panel")[1].split("</form>")[0]

    r = alice.post("/submit/2026/8", {"employee_comment": "both", "back": "overtime"})
    assert r.status_code in (200, 303)
    for kind in ("oncall", "overtime"):
        sheet = _sheets(db, users["alice"], kind)[0]
        assert sheet.status == "submitted" and sheet.employee_comment == "both"
    assert len(mgr.get("/approvals").text.split("Alice")) >= 3  # two sheets pending for the manager

    # nothing left to submit
    alice.post("/submit/2026/8", {})  # nothing left: refused with a message, state unchanged
    assert _sheets(db, users["alice"], "overtime")[0].status == "submitted"


def test_oncall_page_submit_button_sends_overtime_too(browser, db, users):
    alice = browser("alice")
    alice.post("/overtime/entries", _ot("2026-08-11", "18:00", "19:00"))
    alice.post("/oncall/2026/8", {"oc_2026-08-12": "1", "action": "submit"})
    assert _sheets(db, users["alice"], "oncall")[0].status == "submitted"
    assert _sheets(db, users["alice"], "overtime")[0].status == "submitted"


def test_refused_sheet_blocks_whole_submission(browser, db, users):
    alice = browser("alice")
    alice.post("/oncall/2026/8", {"oc_2026-08-12": "1", "action": "save"})
    # weekend overtime, not on an on-call day and not pre-approved -> refused at submission
    alice.post("/overtime/entries", _ot("2026-08-08", "10:00", "11:00", preapproved="1"))
    db.expire_all()
    entry = _sheets(db, users["alice"], "overtime")[0].overtime_entries[0]
    entry.preapproved = False
    db.commit()
    alice.post("/submit/2026/8", {})
    assert [s.status for s in _sheets(db, users["alice"], "oncall")] == ["draft"]
    assert [s.status for s in _sheets(db, users["alice"], "overtime")] == ["draft"]


def test_signed_once_for_both_sheets_and_saved_picture(browser, db, users):
    alice, admin = browser("alice"), browser("admin")
    admin.post("/admin/settings", {"show_amounts": "1", "til_enabled": "1", "signature_required": "1"})
    alice.post("/oncall/2026/8", {"oc_2026-08-10": "1", "action": "save"})
    alice.post("/overtime/entries", _ot("2026-08-10", "18:00", "19:00"))
    assert "js-signature-file" in alice.get("/overtime/2026/8").text
    assert "data-saved-url=\"\"" in alice.get("/overtime/2026/8").text
    assert alice.get("/signature/saved.png").status_code == 404

    alice.post("/submit/2026/8", {"signature": SIG, "signature_confirm": "1", "save_signature": "1"})
    db.expire_all()
    assert db.query(Signature).count() == 2  # one per sheet, same picture
    assert db.get(SavedSignature, users["alice"].id) is not None
    assert alice.get("/signature/saved.png").headers["content-type"] == "image/png"
    assert browser("carol").get("/signature/saved.png").status_code == 404  # private to its owner
    alice.post("/overtime/entries", _ot("2026-07-10", "18:00", "19:00"))  # a new draft shows the panel again
    assert 'data-saved-url="/signature/saved.png"' in alice.get("/overtime/2026/7").text

    alice.post("/signature/forget", {})
    db.expire_all()
    assert db.get(SavedSignature, users["alice"].id) is None


def test_invalid_picture_not_saved(browser, db, users):
    alice, admin = browser("alice"), browser("admin")
    admin.post("/admin/settings", {"show_amounts": "1", "til_enabled": "1", "signature_required": "1"})
    alice.post("/oncall/2026/8", {"oc_2026-08-10": "1", "action": "save"})
    alice.post("/submit/2026/8", {"signature": "data:image/png;base64,AAAA", "signature_confirm": "1", "save_signature": "1"})
    db.expire_all()
    assert db.get(SavedSignature, users["alice"].id) is None
    assert _sheets(db, users["alice"], "oncall")[0].status == "draft"


def test_months_are_synced_between_oncall_and_overtime(browser, users):
    alice = browser("alice")
    alice.get("/oncall/2026/3")
    assert alice.get("/overtime", follow_redirects=False).headers["location"] == "/overtime/2026/3"
    alice.get("/overtime/2026/5")  # manual change on one page moves both
    assert alice.get("/oncall", follow_redirects=False).headers["location"] == "/oncall/2026/5"


def test_current_month_button(browser, users):
    import datetime as dt
    today = dt.date.today()
    alice = browser("alice")
    for kind in ("oncall", "overtime"):
        assert "Current month" not in alice.get(f"/{kind}/{today.year}/{today.month}").text
        page = alice.get(f"/{kind}/2025/3").text
        assert f'href="/{kind}/{today.year}/{today.month}"' in page and "Current month" in page


def test_admin_documentation_page(browser, users):
    page = browser("admin").get("/admin/docs")
    assert page.status_code == 200
    for anchor in ('id="ssl"', 'id="ldap"', "openssl pkcs12", "certs/server.crt", "Test connection"):
        assert anchor in page.text
    assert "Documentation" in browser("admin").get("/admin/users").text  # in the admin sub-navigation
    assert browser("alice").get("/admin/docs").status_code in (403, 404)


def test_oncall_week_start_is_configurable(browser, db, users):
    admin, alice = browser("admin"), browser("alice")
    page = alice.get("/oncall/2026/8").text
    assert "Friday 18:00" in page  # default
    admin.post("/admin/settings", {"show_amounts": "1", "til_enabled": "1", "week_start": "0", "handover": "08:00"})
    page = alice.get("/oncall/2026/8").text
    assert "Monday 08:00" in page and "Monday to Sunday" in page
    # the week switch now books Monday 2026-08-03 .. Sunday 2026-08-09
    alice.post("/oncall/2026/8", {"wk_2026-08-03": "1", "action": "save"})
    from timelog.models import OnCallDay
    db.expire_all()
    assert sorted(d.date.isoformat() for d in db.query(OnCallDay).all()) == [f"2026-08-0{i}" for i in range(3, 10)]
    admin.post("/admin/settings", {"week_start": "9", "handover": "08:00"})  # invalid: ignored
    assert "Monday 08:00" in alice.get("/oncall/2026/8").text


def test_oncall_conflicts_highlighted_for_managers_only(browser, db, users):
    alice, carol, mgr, head, bob = (browser(n) for n in ("alice", "carol", "manager", "head", "bob"))
    # alice and carol both book 2026-08-10 and 11; bob (expert, same manager) books 2026-08-20 alone
    alice.post("/oncall/2026/8", {"oc_2026-08-10": "1", "oc_2026-08-11": "1", "action": "submit"})
    carol.post("/oncall/2026/8", {"oc_2026-08-11": "1", "oc_2026-08-12": "1", "action": "save"})
    assert "conflict" not in mgr.get(f"/oncall/2026/8?user_id={users['alice'].id}").text.lower().replace("on-call conflict", "")
    carol.post("/oncall/2026/8", {"oc_2026-08-11": "1", "oc_2026-08-12": "1", "action": "submit"})  # now both declared

    page = mgr.get(f"/oncall/2026/8?user_id={users['alice'].id}").text
    assert "On-call conflict" in page and "Carol" in page.title() and "11/08/2026" in page
    assert page.count("⚠ conflict") == 1  # only the 11th overlaps, the 10th does not
    assert "1 day(s) in conflict" in mgr.get("/approvals").text

    # the employees themselves never see it
    assert "On-call conflict" not in alice.get("/oncall/2026/8").text
    assert "conflict" not in carol.get("/oncall/2026/8").text.lower().replace("on-call conflict", "")

    # a manager above sees both (head sees alice's sheet and names carol: both are below head)
    assert "Carol" in head.get(f"/oncall/2026/8?user_id={users['alice'].id}").text.title()


def test_conflict_with_unseen_employee_is_anonymous(browser, db, users):
    alice, dave, mgr = browser("alice"), browser("dave"), browser("manager")
    alice.post("/oncall/2026/8", {"oc_2026-08-17": "1", "action": "submit"})
    dave.post("/oncall/2026/8", {"oc_2026-08-17": "1", "action": "submit"})  # dave reports to head, not to manager
    page = mgr.get(f"/oncall/2026/8?user_id={users['alice'].id}").text
    assert "1 other employee outside your team" in page
    banner = page.split("On-call conflict")[1].split("</ul>")[0]
    assert "dave" not in banner.lower()  # an employee outside the manager's scope is never named
