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
