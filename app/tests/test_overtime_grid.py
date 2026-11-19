from timelog.models import Sheet


def _sheets(db, user):
    db.expire_all()
    return db.query(Sheet).filter_by(user_id=user.id, kind="overtime", year=2026, month=8).order_by(Sheet.seq).all()


def _grid(rows):
    """rows: (row_id, date, start, end, incident, description) -> parallel form lists."""
    keys = ["row_id", "date", "start", "end", "incident_ref", "description"]
    data = {k: [r[i] for r in rows] for i, k in enumerate(keys)}
    data["compensation"] = ["PAID"] * len(rows)
    return data


def test_grid_page_has_one_row_per_day_and_new_labels(browser, users):
    page = browser("alice").get("/overtime/2026/8").text
    assert page.count('name="date"') == 31 and "ServiceNow #" in page and "Description of Intervention" in page
    assert "Incident #" not in page and "pre-approved" not in page.lower()


def test_grid_saves_rows_and_second_session_same_day(browser, db, users):
    alice = browser("alice")
    alice.post("/overtime/2026/8/save", _grid([
        ("", "2026-08-10", "18:00", "19:00", "INC1", "First call"),
        ("", "2026-08-10", "22:00", "23:00", "INC2", "Second call"),
        ("", "2026-08-11", "", "", "", ""),
    ]))
    entries = _sheets(db, users["alice"])[0].overtime_entries
    assert sorted((e.start_minute, e.incident_ref) for e in entries) == [(1080, "INC1"), (1320, "INC2")]
    page = alice.get("/overtime/2026/8").text
    assert "First call" in page and "Second call" in page


def test_grid_edit_clear_and_all_or_nothing(browser, db, users):
    alice = browser("alice")
    alice.post("/overtime/2026/8/save", _grid([("", "2026-08-10", "18:00", "19:00", "INC1", "A"),
                                               ("", "2026-08-12", "09:00", "10:00", "", "B")]))
    a, b = sorted(_sheets(db, users["alice"])[0].overtime_entries, key=lambda e: e.date)
    # change A, clear B
    alice.post("/overtime/2026/8/save", _grid([(str(a.id), "2026-08-10", "18:00", "20:00", "INC1", "A"),
                                               (str(b.id), "2026-08-12", "", "", "", "")]))
    entries = _sheets(db, users["alice"])[0].overtime_entries
    assert [(e.date.day, float(e.hours)) for e in entries] == [(10, 2.0)]
    # an invalid row (bad step) refuses the whole save
    alice.post("/overtime/2026/8/save", _grid([("", "2026-08-13", "10:00", "11:00", "", "ok"),
                                               ("", "2026-08-14", "10:10", "11:00", "", "bad")]))
    assert len(_sheets(db, users["alice"])[0].overtime_entries) == 1
    # a weekend day needs no pre-approval
    alice.post("/overtime/2026/8/save", _grid([("", "2026-08-16", "10:00", "12:00", "", "Sunday work")]))
    assert len(_sheets(db, users["alice"])[0].overtime_entries) == 2


def test_grid_refused_for_expert_and_when_submitted(browser, db, users):
    browser("bob").post("/overtime/2026/8/save", _grid([("", "2026-08-10", "18:00", "19:00", "", "x")]))
    assert _sheets(db, users["bob"]) == []
    alice = browser("alice")
    alice.post("/overtime/2026/8/save", _grid([("", "2026-08-10", "18:00", "19:00", "", "x")]))
    alice.post("/submit/2026/8")
    alice.post("/overtime/2026/8/save", _grid([("", "2026-08-11", "18:00", "19:00", "", "y")]))
    assert len(_sheets(db, users["alice"])[0].overtime_entries) == 1


def test_refused_save_keeps_what_the_user_typed(browser, db, users):
    alice = browser("alice")
    r = alice.post("/overtime/2026/8/save", _grid([
        ("", "2026-08-10", "18:00", "19:00", "INC77", "Fine row"),
        ("", "2026-08-10", "21:00", "22:00", "INC88", ""),          # description forgotten
    ]))
    assert r.status_code == 200 and "Nothing was saved" in r.text
    assert 'value="INC77"' in r.text and 'value="INC88"' in r.text and 'value="Fine row"' in r.text
    assert 'value="21:00"' in r.text and r.text.count('name="date"') == 2
    assert _sheets(db, users["alice"]) == []


def test_clearing_times_but_keeping_text_is_refused_and_no_empty_sheet(browser, db, users):
    alice = browser("alice")
    alice.post("/overtime/2026/8/save", _grid([("", "2026-08-10", "18:00", "19:00", "", "A")]))
    e = _sheets(db, users["alice"])[0].overtime_entries[0]
    r = alice.post("/overtime/2026/8/save", _grid([(str(e.id), "2026-08-10", "", "", "", "kept text")]))
    assert "Nothing was saved" in r.text and len(_sheets(db, users["alice"])[0].overtime_entries) == 1
    # approved month + a delete-only post must not open an empty supplementary sheet
    s = _sheets(db, users["alice"])[0]
    s.status = "approved"
    db.commit()
    alice.post("/overtime/2026/8/save", _grid([(str(e.id), "2026-08-10", "", "", "", "")]))
    assert len(_sheets(db, users["alice"])) == 1


def test_payroll_deadline_banner(browser, db, users):
    import datetime as dt
    from timelog import workflow
    from timelog.config import get_settings
    page = browser("alice").get("/").text
    assert "Payroll Deadline:" in page
    # default cut-off = the 5th of the following month
    b = workflow.cutoff_banner(db, get_settings(), dt.date(2026, 11, 2))
    assert b["deadline"] == dt.date(2026, 11, 5) and b["days"] == 3 and not b["late"]
    late = workflow.cutoff_banner(db, get_settings(), dt.date(2026, 11, 19))   # October's cut-off has passed
    assert late["deadline"] == dt.date(2026, 12, 5) and late["late"] and late["paid_with"] == "November 2026"


def test_grid_limits_rows_and_echoed_input(browser, db, users):
    alice = browser("alice")
    many = [("", "2026-08-10", "", "", "", "")] * 101
    r = alice.post("/overtime/2026/8/save", _grid(many))
    assert "Too many rows" in r.text or "Too many rows" in alice.get("/overtime/2026/8").text
    alice.post("/overtime/2026/8/save", _grid([("", "x" * 5000, "10:00", "11:00", "", "d")]))
    page = alice.get("/overtime/2026/8").text
    assert "x" * 100 not in page and _sheets(db, users["alice"]) == []
