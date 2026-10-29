import datetime as dt
import io

import pytest

from timelog.calendar_utils import rotation_week_start
from timelog.ics_import import IcsError, parse_ics
from timelog.models import Holiday, Sheet


def _days(db, user, year, month):
    db.expire_all()
    s = db.query(Sheet).filter_by(user_id=user.id, kind="oncall", year=year, month=month).one_or_none()
    return sorted(d.date.day for d in s.oncall_days) if s else []


# ---------------------------------------------------------------- rotation weeks
def test_rotation_week_starts_friday():
    assert rotation_week_start(dt.date(2026, 8, 13), 4) == dt.date(2026, 8, 7)   # Thu -> previous Fri
    assert rotation_week_start(dt.date(2026, 8, 14), 4) == dt.date(2026, 8, 14)  # Fri


def test_week_toggle_books_friday_to_thursday(browser, db, users):
    browser("alice").post("/oncall/2026/8", {"wk_2026-08-07": "1", "action": "save"})
    assert _days(db, users["alice"], 2026, 8) == [7, 8, 9, 10, 11, 12, 13]


def test_week_toggle_spills_into_next_month(browser, db, users):
    alice = browser("alice")
    r = alice.post("/oncall/2026/8", {"wk_2026-08-28": "1", "action": "save"})
    assert r.status_code == 303
    assert _days(db, users["alice"], 2026, 8) == [28, 29, 30, 31]
    assert _days(db, users["alice"], 2026, 9) == [1, 2, 3]
    # the toggle is shown "on" since the full week is booked across both months
    page = alice.get("/oncall/2026/8").text
    assert 'name="wk_2026-08-28" value="1" data-week="2026-08-28" data-spill="1" checked' in page


def test_week_spill_blocked_when_other_month_submitted(browser, db, users):
    alice = browser("alice")
    alice.post("/oncall/2026/9", {"oc_2026-09-10": "1", "action": "submit"})
    alice.post("/oncall/2026/8", {"wk_2026-08-28": "1", "action": "save"})
    assert _days(db, users["alice"], 2026, 8) == [28, 29, 30, 31]
    assert _days(db, users["alice"], 2026, 9) == [10]  # submitted sheet untouched


def test_week_from_previous_month_spills_back(browser, db, users):
    # Week Fri 31/07 -> Thu 06/08 toggled from the August page
    browser("alice").post("/oncall/2026/8", {"wk_2026-07-31": "1", "action": "save"})
    assert _days(db, users["alice"], 2026, 8) == [1, 2, 3, 4, 5, 6]
    assert _days(db, users["alice"], 2026, 7) == [31]


# ---------------------------------------------------------------- amounts visibility
def test_amounts_hidden_from_employees_when_disabled(browser, db, users):
    alice, admin, mgr = browser("alice"), browser("admin"), browser("manager")
    alice.post("/oncall/2026/8", {"oc_2026-08-10": "1", "action": "save"})
    assert "data-rates='{" in alice.get("/oncall/2026/8").text

    assert browser("carol").get("/admin/settings").status_code == 404
    admin.post("/admin/settings", {})  # checkbox unticked -> hide
    page = alice.get("/oncall/2026/8").text
    assert "data-rates=''" in page and "Stipend" not in page and "45.00" not in page
    assert "On-call stipend" not in alice.get("/").text
    # line manager and admin still see the employee's amounts
    for viewer in (mgr, admin):
        assert "45.00" in viewer.get(f"/oncall/2026/8?user_id={users['alice'].id}").text

    admin.post("/admin/settings", {"show_amounts": "1"})
    assert "data-rates='{" in alice.get("/oncall/2026/8").text


# ---------------------------------------------------------------- ICS import
ICS = b"""BEGIN:VCALENDAR\r
VERSION:2.0\r
PRODID:-//test//EN\r
BEGIN:VEVENT\r
UID:1\r
DTSTART;VALUE=DATE:20270101\r
DTEND;VALUE=DATE:20270102\r
SUMMARY:Jour de l'an\r
END:VEVENT\r
BEGIN:VEVENT\r
UID:2\r
DTSTART;VALUE=DATE:20271225\r
DTEND;VALUE=DATE:20271227\r
SUMMARY:No\\u00ebl \\, Saint-\r
 Etienne\r
END:VEVENT\r
BEGIN:VEVENT\r
UID:3\r
DTSTART:20270623T000000Z\r
SUMMARY:Fete nationale\r
END:VEVENT\r
BEGIN:VEVENT\r
UID:4\r
DTSTART;VALUE=DATE:20270501\r
RRULE:FREQ=YEARLY\r
SUMMARY:Labour day (recurring)\r
END:VEVENT\r
END:VCALENDAR\r
"""


def test_parse_ics():
    r = parse_ics(ICS)
    assert sorted(r.holidays) == [dt.date(2027, 1, 1), dt.date(2027, 6, 23), dt.date(2027, 12, 25), dt.date(2027, 12, 26)]
    assert r.holidays[dt.date(2027, 12, 26)].endswith(", Saint-Etienne")  # unfolded + unescaped
    assert r.skipped_recurring == 1 and r.years == [2027]
    with pytest.raises(IcsError):
        parse_ics(b"hello")


def test_import_ics_merge_and_replace(browser, db, users):
    admin = browser("admin")
    db.add(Holiday(date=dt.date(2027, 3, 3), name="Old 2027 entry"))
    db.commit()

    def upload(mode):
        return admin.post("/admin/holidays/import", {"mode": mode},
                          files={"file": ("lu.ics", io.BytesIO(ICS), "text/calendar")})

    assert browser("alice").post("/admin/holidays/import", {"mode": "merge"},
                                 files={"file": ("lu.ics", io.BytesIO(ICS), "text/calendar")}).status_code == 404
    upload("merge")
    db.expire_all()
    dates_2027 = {h.date for h in db.query(Holiday).all() if h.date.year == 2027}
    assert dt.date(2027, 3, 3) in dates_2027 and len(dates_2027) == 5
    upload("replace")
    db.expire_all()
    dates_2027 = {h.date for h in db.query(Holiday).all() if h.date.year == 2027}
    assert dt.date(2027, 3, 3) not in dates_2027 and len(dates_2027) == 4
    # other years untouched
    assert db.query(Holiday).filter(Holiday.date == dt.date(2026, 8, 15)).count() == 1
