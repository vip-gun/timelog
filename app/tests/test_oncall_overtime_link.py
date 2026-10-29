"""Non-experts file intervention details on the overtime sheet only; on-call days are detected automatically."""
from timelog.models import Sheet


def _ot(date, start, end, **extra):
    return {"date": date, "start": start, "end": end, "description": "Incident fix", **extra}


def _sheet(db, user, kind, month=8):
    db.expire_all()
    return db.query(Sheet).filter_by(user_id=user.id, kind=kind, year=2026, month=month).order_by(Sheet.seq.desc()).first()


def test_non_expert_oncall_grid_has_no_incident_or_comment(browser, db, users):
    alice = browser("alice")
    page = alice.get("/oncall/2026/8").text
    assert 'name="in_2026-08-10"' not in page and 'name="cm_2026-08-10"' not in page and 'name="iv_2026-08-10"' not in page
    # values posted anyway are ignored for non-experts
    alice.post("/oncall/2026/8", {"oc_2026-08-10": "1", "iv_2026-08-10": "1", "in_2026-08-10": "INC1",
                                  "cm_2026-08-10": "x", "action": "save"})
    day = _sheet(db, users["alice"], "oncall").oncall_days[0]
    assert (day.intervention, day.incident_ref, day.comment) == (False, "", "")


def test_expert_keeps_incident_and_comment(browser, db, users):
    bob = browser("bob")
    assert 'name="in_2026-08-10"' in bob.get("/oncall/2026/8").text
    bob.post("/oncall/2026/8", {"oc_2026-08-10": "1", "iv_2026-08-10": "1", "in_2026-08-10": "INC1", "action": "save"})
    day = _sheet(db, users["bob"], "oncall").oncall_days[0]
    assert day.intervention and day.incident_ref == "INC1" and day.amount == 90


def test_overtime_on_oncall_day_detected_automatically(browser, db, users):
    alice = browser("alice")
    alice.post("/oncall/2026/8", {"wk_2026-08-07": "1", "action": "save"})  # Fri 7 -> Thu 13
    # Sunday 9 Aug: allowed without pre-approval because it is an on-call day
    alice.post("/overtime/entries", _ot("2026-08-09", "02:00", "03:30", incident_ref="INC42"))
    entry = _sheet(db, users["alice"], "overtime").overtime_entries[0]
    assert entry.on_call_intervention and entry.incident_ref == "INC42"
    # the on-call grid shows the hours logged that day
    assert "1.50 h" in alice.get("/oncall/2026/8").text
    # the form knows the on-call dates for the live hint
    assert "2026-08-09" in alice.get("/overtime/2026/8").text.split('data-oncall-dates="')[1].split('"')[0]


def test_weekend_overtime_outside_oncall_needs_preapproval(browser, db, users):
    alice = browser("alice")
    alice.post("/overtime/entries", _ot("2026-08-16", "10:00", "12:00"))  # Sunday, not on-call
    assert _sheet(db, users["alice"], "overtime") is None
    alice.post("/overtime/entries", _ot("2026-08-16", "10:00", "12:00", preapproved="1"))
    entry = _sheet(db, users["alice"], "overtime").overtime_entries[0]
    assert entry.preapproved and not entry.on_call_intervention


def test_submit_rechecks_oncall_days(browser, db, users):
    alice = browser("alice")
    alice.post("/oncall/2026/8", {"oc_2026-08-09": "1", "action": "save"})
    alice.post("/overtime/entries", _ot("2026-08-09", "10:00", "11:00"))
    alice.post("/oncall/2026/8", {"action": "save"})  # on-call day removed afterwards
    sheet = _sheet(db, users["alice"], "overtime")
    alice.post(f"/submit/{sheet.year}/{sheet.month}")
    assert _sheet(db, users["alice"], "overtime").status == "draft"  # refused: Sunday no longer justified
    alice.post("/oncall/2026/8", {"oc_2026-08-09": "1", "action": "save"})
    alice.post(f"/submit/{sheet.year}/{sheet.month}")
    sheet = _sheet(db, users["alice"], "overtime")
    assert sheet.status == "submitted" and sheet.overtime_entries[0].on_call_intervention
