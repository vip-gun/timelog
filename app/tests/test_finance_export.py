"""Finance exports in the layout of the on-call sheet and the overtime sheet."""
import datetime as dt
import io

from openpyxl import load_workbook

from timelog.models import Sheet

from test_v1_features import _ot


def _approved_august(browser, db, users):
    alice, bob, mgr = browser("alice"), browser("bob"), browser("manager")
    alice.post("/oncall/2026/8", {"oc_2026-08-10": "1", "oc_2026-08-15": "1", "action": "submit"})
    bob.post("/oncall/2026/8", {"oc_2026-08-10": "1", "iv_2026-08-10": "1", "oc_2026-08-11": "1", "action": "submit"})
    alice.post("/overtime/entries", _ot("2026-08-10", "18:00", "21:00"))
    alice.post("/overtime/entries", _ot("2026-08-10", "22:00", "23:45", compensation="TIL"))
    alice.post("/overtime/entries", _ot("2026-08-09", "10:00", "12:30", preapproved="1"))   # Sunday
    alice.post("/submit/2026/8")
    db.expire_all()
    for s in db.query(Sheet).filter(Sheet.status == "submitted"):
        mgr.post(f"/sheets/{s.id}/decide", {"action": "approve"})
    db.expire_all()
    s = db.query(Sheet).filter_by(user_id=users["alice"].id).first()
    return mgr, s.payroll_year, s.payroll_month


def test_oncall_export_follows_the_template_layout(browser, db, users):
    mgr, py, pm = _approved_august(browser, db, users)
    r = browser("admin").get(f"/payroll/export/oncall?year={py}&month={pm}")
    assert r.status_code == 200
    assert r.headers["content-disposition"].endswith(f'Y{py % 100:02d}M{pm:02d}_IT_OnCall.xlsx"')
    wb = load_workbook(io.BytesIO(r.content))
    assert wb["Support"].sheet_state == "hidden"
    ws = wb["AUG (late)"]                      # approved after the cut-off: late claim tab of its work month
    assert (ws["B3"].value, ws["C3"].value, ws["C4"].value, ws["C5"].value) == ("ON CALL ", "Date", "Week day", "Type")
    assert ws["D3"].value == dt.datetime(2026, 8, 1) and ws["D4"].value == "Sat" and ws["D5"].value == "Sat"
    assert ws["R5"].value == "H"               # 15 August: legal holiday wins over Saturday
    col = {name: next(c for c in range(4, 35) if ws.cell(3, c).value == dt.datetime(2026, 8, d)) for name, d in
           (("10", 10), ("11", 11), ("15", 15))}
    title = next(r for r in range(6, 120) if ws.cell(r, 4).value and "PRICING SHEET" in str(ws.cell(r, 4).value))
    rows = {ws.cell(r, 2).value: r for r in range(6, title) if ws.cell(r, 2).value}
    alice_row = next(r for n, r in rows.items() if n.startswith("Alice") and "Incidents" not in n)
    bob_row = next(r for n, r in rows.items() if n.startswith("Bob") and "Incidents" not in n)
    inc_row = next(r for n, r in rows.items() if n.startswith("Bob") and "Incidents/calls" in n)
    assert ws.cell(alice_row, col["10"]).value == 1 and ws.cell(alice_row, col["15"]).value == 1 and ws.cell(alice_row, 35).value == 2
    assert inc_row == bob_row + 1 and ws.cell(inc_row, col["10"]).value == 1 and ws.cell(inc_row, col["11"]) .value is None
    # pricing block: the same rows 60 lower (here: below the employee block), amounts = snapshotted rates, intervention counted twice
    shift = title                                   # pricing row = employee row + shift
    assert ws.cell(title + 4, 3).value == "Price" and ws.cell(title + 4, 4 + 9).value == 45.0     # Monday 10 Aug
    assert ws.cell(alice_row + shift, 2).value == ws.cell(alice_row, 2).value
    assert ws.cell(alice_row + shift, col["15"]).value == 90.0                                     # holiday rate
    assert ws.cell(bob_row + shift, 35).value + ws.cell(inc_row + shift, 35).value == 45.0 * 3
    # the period month itself has a tab (empty here) and the late-claim tab comes from the work month
    assert any(n.startswith(("OCT", "NOV", "SEP")) for n in wb.sheetnames if n != "Support")


def test_overtime_export_follows_the_template_layout(browser, db, users):
    mgr, py, pm = _approved_august(browser, db, users)
    r = browser("admin").get(f"/payroll/export/overtime?year={py}&month={pm}")
    assert r.status_code == 200 and r.headers["content-disposition"].endswith("_IT_Overtime.xlsx\"")
    wb = load_workbook(io.BytesIO(r.content))
    assert wb.sheetnames[0] == "READ FIRST" and "AUG 2026 (late)" in wb.sheetnames
    ws = wb["AUG 2026 (late)"]
    hdr = {ws.cell(3, c).value: c for c in range(1, 52) if ws.cell(3, c).value}
    assert ws["A3"].value == "LAST Name" and ws["B3"].value == "First Name" and ws["C3"].value == "Add PAID or TIL"
    for title in ("HL100 Normal", "HL101 Normal 50%", "HL111 Night 30%", "HL130 Sunday 70%", "HL121 Legal 200%", "TIL (no payment)"):
        assert title in hdr
    assert ws["D3"].value == "Sa" and ws["E3"].value == "Sun"
    assert ws["R3"].value == "Sa\n LH"                                     # 15 August is a legal holiday
    assert ws["D2"].value == dt.datetime(2026, 8, 1)

    lines = [[c.value for c in row] for row in ws.iter_rows(min_row=4, max_row=12, max_col=52)]
    lines = [ln for ln in lines if ln[0]]
    assert [(ln[1], ln[2]) for ln in lines] == [("Alice", "Paid"), ("Alice", "TIL")]   # one line per employee and compensation
    paid, til = lines
    day = lambda d: 2 + d                                                           # day number -> list index (column D = day 1)
    assert paid[day(9)] == 2.5 and paid[day(10)] == 3.0 and paid[day(11)] is None   # Sunday 9th, Monday 10th
    # AI..AM: weekday, Saturday, Sunday, legal holiday, total
    assert paid[34:39] == [3.0, 0.0, 2.5, 0.0, 5.5]
    assert paid[39:43] == [0.0, 0.0, 0.0, 0.0]                                       # AN..AQ: no night hours
    assert paid[43:50] == [0.0, 5.5, 5.5, 0.0, 2.5, 0.0, 0.0]                        # AR, HL100, HL101, HL111, HL130, HL121, TIL
    # TIL line: 22:00-23:45 is all night: 1.75 h x 1.8
    assert til[day(10)] == 1.75 and til[34] == 1.75 and til[38] == 1.75 and til[39] == 1.75
    assert til[43] == 3.15 and til[49] == 3.15 and til[44:49] == [0.0] * 5
    assert ws.data_validations.dataValidation[0].formula1 == "$AY$2:$AZ$2"
    assert any("NOTE FOR NON-RESIDENTS" in str(c.value) for row in ws.iter_rows(min_row=5, max_row=20) for c in row if c.value)
    assert wb["READ FIRST"]["C11"].value is not None and "utmost" not in str([c.value for r in wb["READ FIRST"].iter_rows() for c in r]).lower()
