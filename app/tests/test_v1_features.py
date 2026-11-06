"""v1.0: tab title, time-in-lieu switch, signatures on submission."""
import base64
import io
import struct
import zlib

from openpyxl import load_workbook

from timelog import __version__
from timelog.models import Sheet, Signature
from timelog.signatures import decode_png_data_url, verify


def _png(width=40, height=12) -> bytes:
    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)
    raw = b"".join(b"\x00" + b"\xff" * width * 3 for _ in range(height))
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))


SIG = "data:image/png;base64," + base64.b64encode(_png()).decode()


def _ot(date, start, end, **extra):
    return {"date": date, "start": start, "end": end, "description": "Work", **extra}


def _sheets(db, user, kind):
    db.expire_all()
    return db.query(Sheet).filter_by(user_id=user.id, kind=kind).order_by(Sheet.id).all()


def test_version_and_tab_title(browser, users):
    assert __version__ == "1.11.1"
    assert "<title>Timelog - Dashboard</title>" in browser("alice").get("/").text
    assert "<title>Timelog - Sign In</title>" in browser().get("/login").text


# ---------------------------------------------------------------- time in lieu switch
def test_til_disabled_hides_and_refuses(browser, db, users):
    alice, admin = browser("alice"), browser("admin")
    assert 'value="TIL"' in alice.get("/overtime/2026/8").text
    admin.post("/admin/settings", {"show_amounts": "1"})  # TIL off, signature off
    page = alice.get("/overtime/2026/8").text
    assert 'value="TIL"' not in page and 'name="compensation" value="PAID"' in page
    alice.post("/overtime/entries", _ot("2026-08-10", "18:00", "19:00", compensation="TIL"))
    assert _sheets(db, users["alice"], "overtime") == []  # refused server-side
    alice.post("/overtime/entries", _ot("2026-08-10", "18:00", "19:00"))
    sheet = _sheets(db, users["alice"], "overtime")[0]
    assert sheet.overtime_entries[0].compensation == "PAID"
    assert "TIL credit" not in alice.get("/overtime/2026/8").text
    assert "TIL factors" not in admin.get("/admin/settings").text.split("subnav")[1].split("</nav>")[0]

    # export has no TIL columns when there is no TIL data
    alice.post(f"/submit/{sheet.year}/{sheet.month}")
    browser("manager").post(f"/sheets/{sheet.id}/decide", {"action": "approve"})
    db.expire_all()
    s = db.get(Sheet, sheet.id)
    wb = load_workbook(io.BytesIO(browser("admin").get(f"/payroll/export/control?year={s.payroll_year}&month={s.payroll_month}").content))
    headers = [c.value for c in wb["Overtime Payroll"][1]]
    assert "TIL credit (no payment)" not in headers and headers[-1] == "Signed (UTC)"


def test_til_disabled_blocks_submitting_existing_til_draft(browser, db, users):
    alice, admin = browser("alice"), browser("admin")
    alice.post("/overtime/entries", _ot("2026-08-10", "18:00", "19:00", compensation="TIL"))
    admin.post("/admin/settings", {"show_amounts": "1"})
    sheet = _sheets(db, users["alice"], "overtime")[0]
    assert "TIL credit" in alice.get("/overtime/2026/8").text  # existing TIL data stays visible
    alice.post(f"/submit/{sheet.year}/{sheet.month}")
    assert _sheets(db, users["alice"], "overtime")[0].status == "draft"


# ---------------------------------------------------------------- signatures
def test_decode_signature_validation():
    assert decode_png_data_url(SIG).startswith(b"\x89PNG")
    for bad in ("", "data:image/jpeg;base64,AAAA", "data:image/png;base64,!!!", "data:image/png;base64,"
                + base64.b64encode(b"not a png at all, definitely not").decode()):
        try:
            decode_png_data_url(bad)
            raise AssertionError(bad)
        except ValueError:
            pass


def test_signature_required_on_submission(browser, db, users):
    alice, admin, mgr = browser("alice"), browser("admin"), browser("manager")
    admin.post("/admin/settings", {"show_amounts": "1", "til_enabled": "1", "signature_required": "1"})
    assert "js-signature-pad" in alice.get("/oncall/2026/8").text

    # on-call: submit without signature -> saved as draft, not submitted
    alice.post("/oncall/2026/8", {"oc_2026-08-10": "1", "action": "submit"})
    sheet = _sheets(db, users["alice"], "oncall")[0]
    assert sheet.status == "draft"
    # signature without confirmation -> refused
    alice.post("/oncall/2026/8", {"oc_2026-08-10": "1", "action": "submit", "signature": SIG})
    assert _sheets(db, users["alice"], "oncall")[0].status == "draft"
    # signed + confirmed -> submitted with a verified signature
    alice.post("/oncall/2026/8", {"oc_2026-08-10": "1", "action": "submit", "signature": SIG, "signature_confirm": "1"})
    sheet = _sheets(db, users["alice"], "oncall")[0]
    assert sheet.status == "submitted" and sheet.signature is not None and verify(sheet)
    sig_id = sheet.signature.id

    # manager sees the signature and the integrity check; colleague cannot load the image
    page = mgr.get(f"/oncall/2026/8?user_id={users['alice'].id}").text
    assert f"/signatures/{sig_id}.png" in page and "content unchanged since signature" in page
    assert mgr.get(f"/signatures/{sig_id}.png").headers["content-type"] == "image/png"
    assert browser("carol").get(f"/signatures/{sig_id}.png").status_code == 404

    # tampering with the signed content is detected
    day = sheet.oncall_days[0]
    day.amount = 999
    db.commit()
    assert not verify(_sheets(db, users["alice"], "oncall")[0])
    assert "content changed after signature" in mgr.get(f"/oncall/2026/8?user_id={users['alice'].id}").text


def test_signature_on_overtime_and_resubmission(browser, db, users):
    alice, admin, mgr = browser("alice"), browser("admin"), browser("manager")
    admin.post("/admin/settings", {"show_amounts": "1", "til_enabled": "1", "signature_required": "1"})
    alice.post("/overtime/entries", _ot("2026-08-10", "18:00", "19:00"))
    sheet = _sheets(db, users["alice"], "overtime")[0]
    alice.post(f"/submit/{sheet.year}/{sheet.month}")
    assert _sheets(db, users["alice"], "overtime")[0].status == "draft"
    alice.post(f"/submit/{sheet.year}/{sheet.month}", {"signature": SIG, "signature_confirm": "1"})
    assert _sheets(db, users["alice"], "overtime")[0].status == "submitted"
    mgr.post(f"/sheets/{sheet.id}/decide", {"action": "reject", "comment": "fix it"})
    sheet = _sheets(db, users["alice"], "overtime")[0]
    assert sheet.status == "rejected" and sheet.signature is None  # previous signature no longer current
    alice.post(f"/submit/{sheet.year}/{sheet.month}", {"signature": SIG, "signature_confirm": "1"})
    db.expire_all()
    assert db.query(Signature).filter_by(sheet_id=sheet.id).count() == 2  # history kept
    mgr.post(f"/sheets/{sheet.id}/decide", {"action": "approve"})
    s = _sheets(db, users["alice"], "overtime")[0]
    assert s.status == "approved" and verify(s)
    wb = load_workbook(io.BytesIO(browser("admin").get(f"/payroll/export/control?year={s.payroll_year}&month={s.payroll_month}").content))
    row = [r for r in wb["Overtime Payroll"].iter_rows(min_row=2, values_only=True) if r[2] == "Alice"][0]
    assert row[-1]  # signed timestamp exported


def test_signature_image_limits(browser, users):
    import pytest
    huge = "data:image/png;base64," + "A" * (500 * 1024)
    with pytest.raises(ValueError, match="too large"):
        decode_png_data_url(huge)
    with pytest.raises(ValueError, match="size"):
        decode_png_data_url("data:image/png;base64," + base64.b64encode(_png(width=3000, height=10)).decode())
    assert browser("alice").get("/signatures/9999.png").status_code == 404


def test_only_owner_submits_or_withdraws_and_approvals_list_overtime(browser, db, users):
    alice, mgr, carol = browser("alice"), browser("manager"), browser("carol")
    alice.post("/overtime/entries", _ot("2026-08-10", "18:00", "19:30"))
    sheet = _sheets(db, users["alice"], "overtime")[0]
    mgr.post(f"/submit/{sheet.year}/{sheet.month}")  # a manager cannot submit on behalf of the employee
    assert _sheets(db, users["alice"], "overtime")[0].status == "draft"
    alice.post(f"/submit/{sheet.year}/{sheet.month}")
    assert "1.50 h" in mgr.get("/approvals").text  # pending overtime summary
    mgr.post(f"/sheets/{sheet.id}/withdraw")  # nor withdraw it
    assert _sheets(db, users["alice"], "overtime")[0].status == "submitted"
    carol.post(f"/submit/{sheet.year}/{sheet.month}")  # a colleague only ever submits their own (empty) month
    assert _sheets(db, users["alice"], "overtime")[0].status == "submitted"
