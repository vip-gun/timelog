import datetime as dt
import io

import pytest
from openpyxl import Workbook

from conftest import PASSWORD  # noqa: F401
from timelog import ooh_report as ooh

HEADER = ["Number", "Short description", "Assigned to", "Planned start date", "Planned end date"]


def _xlsx(rows, as_text=True, header=HEADER):
    wb = Workbook()
    ws = wb.active
    ws.append(header)
    for desc, who, s, e in rows:
        ws.append(["T1", desc, who, s if as_text else dt.datetime.fromisoformat(s),
                   e if as_text else dt.datetime.fromisoformat(e)])
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def _report(rows, holidays=frozenset(), **kw):
    return ooh.build_report(ooh.parse_file(_xlsx(rows), "x.xlsx"), set(holidays), **kw)


def test_business_day_counts_only_outside_working_hours():
    # Wed 2026-03-04: 07-10 -> 2 h before 09:00; 17-20 -> 2 h after 18:00; fully inside hours -> 0
    r = _report([("a", "Bob", "2026-03-04 07:00:00", "2026-03-04 10:00:00"),
                 ("b", "Bob", "2026-03-04 17:00:00", "2026-03-04 20:00:00"),
                 ("c", "Bob", "2026-03-04 11:00:00", "2026-03-04 12:00:00")])
    assert r.totals["Bob"]["W"] == 4.0
    assert r.tickets["Bob"] == 2                      # the in-hours one is not an out-of-hours intervention


def test_weekend_and_holiday_hours_all_count_holiday_wins():
    # Sat 2026-03-07 10-12 (2 h), Sun 2026-03-08 10-11 (1 h), Mon 2026-03-09 is a holiday here: 10-13 (3 h)
    r = _report([("s", "Eve", "2026-03-07 10:00:00", "2026-03-07 12:00:00"),
                 ("u", "Eve", "2026-03-08 10:00:00", "2026-03-08 11:00:00"),
                 ("h", "Eve", "2026-03-09 10:00:00", "2026-03-09 13:00:00")], holidays={dt.date(2026, 3, 9)})
    assert r.totals["Eve"] == {"W": 0.0, "SAT": 2.0, "SUN": 1.0, "H": 3.0}
    # a Saturday that is also a holiday counts as holiday only
    r = _report([("s", "Eve", "2026-03-07 10:00:00", "2026-03-07 12:00:00")], holidays={dt.date(2026, 3, 7)})
    assert r.totals["Eve"]["H"] == 2.0 and r.totals["Eve"]["SAT"] == 0.0


def test_crossing_midnight_splits_by_day_type():
    # Fri 2026-03-06 22:00 -> Sat 02:00: 2 h business-day (after 18:00), 2 h Saturday
    r = _report([("n", "Bob", "2026-03-06 22:00:00", "2026-03-07 02:00:00")])
    assert r.totals["Bob"]["W"] == 2.0 and r.totals["Bob"]["SAT"] == 2.0


def test_overlaps_are_merged_per_person_only():
    r = _report([("a", "Bob", "2026-03-07 10:00:00", "2026-03-07 12:00:00"),
                 ("b", "Bob", "2026-03-07 11:00:00", "2026-03-07 13:00:00"),
                 ("c", "Eve", "2026-03-07 11:00:00", "2026-03-07 13:00:00")])
    assert r.totals["Bob"]["SAT"] == 3.0 and r.overlaps == 1
    assert r.totals["Eve"]["SAT"] == 2.0               # another person's hours are never merged


def test_monthly_trend_fills_gaps_and_custom_hours():
    r = _report([("a", "Bob", "2026-01-10 10:00:00", "2026-01-10 11:00:00"),
                 ("b", "Bob", "2026-03-04 08:00:00", "2026-03-04 09:00:00")], biz_start=9, biz_end=17)
    assert r.months == ["2026-01", "2026-02", "2026-03"]
    assert r.monthly[("Bob", "2026-01")]["SAT"] == 1.0
    assert r.monthly[("Bob", "2026-03")]["W"] == 1.0
    assert ooh.month_label("2026-03") == "Mar 26"


def test_real_excel_datetimes_and_header_variants():
    data = _xlsx([("a", "Bob", "2026-03-07 10:00:00", "2026-03-07 12:00:00")], as_text=False,
                 header=["number", "SHORT_DESCRIPTION", "Assigned To", "Planned start date", "Planned end date"])
    p = ooh.parse_file(data, "x.xlsx")
    assert ooh.build_report(p, set()).totals["Bob"]["SAT"] == 2.0


def test_bad_rows_are_skipped_and_reported():
    r = _report([("ok", "Bob", "2026-03-07 10:00:00", "2026-03-07 12:00:00"),
                 ("rev", "Bob", "2026-03-07 12:00:00", "2026-03-07 10:00:00"),
                 ("none", "", "2026-03-07 10:00:00", "2026-03-07 12:00:00"),
                 ("bad", "Bob", "yesterday", "2026-03-07 12:00:00")])
    assert len(r.parsed.skipped) == 3 and r.totals["Bob"]["SAT"] == 2.0


def test_csv_and_errors():
    csv = "Short description,Assigned to,Planned start date,Planned end date\nx,Bob,2026-03-07 10:00:00,2026-03-07 11:00:00\n"
    assert ooh.parse_file(csv.encode(), "x.csv").items[0].person == "Bob"
    with pytest.raises(ooh.OohError, match="Missing column"):
        ooh.parse_file(b"a,b\n1,2\n", "x.csv")
    with pytest.raises(ooh.OohError):
        ooh.parse_file(b"", "x.csv")
    with pytest.raises(ooh.OohError):
        ooh.parse_file(b"PK\x03\x04garbage", "x.xlsx")
    with pytest.raises(ooh.OohError):
        ooh.build_report(ooh.parse_file(csv.encode(), "x.csv"), set(), 18, 9)


def test_chart_geometry():
    r = _report([("a", "Bob", "2026-01-10 10:00:00", "2026-01-10 14:00:00"),
                 ("b", "Eve", "2026-02-07 10:00:00", "2026-02-07 12:00:00")])
    assert ooh.hbars(r)["rows"][0]["name"] == "Bob"
    assert ooh.nice_max(37) == 50 and ooh.nice_max(0) == 1
    assert len(ooh.person_columns(r)) == 2 and ooh.team_columns(r)["bars"]
    assert ooh.heatmap(r)[0]["cells"][0]["level"] >= 1


# ---------------------------------------------------------------- web
def _upload(b, rows, **form):
    data = {"biz_start": "9", "biz_end": "18", **form}
    return b.post("/analysis", data=data, files={"file": ("export.xlsx", _xlsx(rows), "application/octet-stream")})


ROWS = [("Fix <b>db</b>", "Bob Test", "2026-03-07 10:00:00", "2026-03-07 12:00:00")]


def test_page_access(browser, users):
    assert browser("manager").get("/analysis").status_code == 200       # has direct reports
    assert browser("alice").get("/analysis").status_code == 404         # plain employee
    assert _upload(browser("alice"), ROWS).status_code == 404
    assert browser().get("/analysis", follow_redirects=False).status_code == 303


def test_upload_renders_report_and_escapes(browser, users):
    b = browser("manager")
    r = _upload(b, ROWS)
    assert r.status_code == 200 and "Bob Test" in r.text and "Hours per person" in r.text
    assert "<b>db</b>" not in r.text and "&lt;b&gt;db&lt;/b&gt;" in r.text
    assert "style=" not in r.text                                   # strict CSP: no inline style
    bad = b.post("/analysis", data={"biz_start": "9", "biz_end": "18"}, files={"file": ("x.csv", b"a,b\n1,2\n")})
    assert "Missing column" in bad.text
    assert "No out-of-hours" in _upload(b, [("x", "Bob", "2026-03-04 10:00:00", "2026-03-04 11:00:00")]).text


def test_upload_requires_csrf(browser, users):
    b = browser("manager")
    r = b.client.post("/analysis", data={"biz_start": "9", "biz_end": "18", "csrf_token": "nope"},
                      files={"file": ("x.xlsx", _xlsx(ROWS))})
    assert r.status_code == 400


# ---------------------------------------------------------------- saved datasets
def _save(b, rows=ROWS, name="March export"):
    return b.post("/analysis", data={"biz_start": "9", "biz_end": "18", "save": "1", "name": name},
                  files={"file": ("export.xlsx", _xlsx(rows), "application/octet-stream")})


def test_save_view_combine_edit_and_delete(browser, db, users):
    from timelog.models import AuditLog, OohDataset, OohIntervention
    b = browser("manager")
    assert "Saved as dataset" not in _upload(b, ROWS).text and db.query(OohDataset).count() == 0   # not saved by default
    r = _save(b)
    assert r.status_code == 200 and "March export" in r.text
    db.expire_all()
    ds = db.query(OohDataset).one()
    assert ds.name == "March export" and len(ds.interventions) == 1 and ds.created_by_id == users["manager"].id
    # a second dataset, then both together
    _save(b, [("x", "Alice Test", "2026-03-08 10:00:00", "2026-03-08 12:00:00")], name="Second")
    db.expire_all()
    ids = [d.id for d in db.query(OohDataset).order_by(OohDataset.id)]
    both = b.get(f"/analysis/view?ids={ids[0]}&ids={ids[1]}").text
    assert "Bob Test" in both and "Alice Test" in both and "Hours per person" in both               # both panes, one request
    assert "Select at least one" in b.get("/analysis/view", follow_redirects=True).text
    # edit a row, add one, delete one
    row, dsid = ds.interventions[0], ds.id
    page = b.get(f"/analysis/datasets/{ds.id}").text
    assert "Bob Test" in page and 'value="Delete dataset"' not in page
    b.post(f"/analysis/datasets/{ds.id}/rows/{row.id}", {"person": "Bob Renamed", "description": "d",
                                                    "start": "2026-03-07 10:00:00", "end": "2026-03-07 13:00:00"})
    db.expire_all()
    assert db.get(OohIntervention, row.id).person == "Bob Renamed"
    b.post(f"/analysis/datasets/{ds.id}/rows/{row.id}", {"person": "Bob", "description": "", "start": "bad", "end": "x"})
    assert db.get(OohIntervention, row.id).person == "Bob Renamed"                                  # invalid: refused
    b.post(f"/analysis/datasets/{ds.id}/rows", {"person": "New", "description": "n", "start": "2026-03-14 09:00:00",
                                           "end": "2026-03-14 10:00:00"})
    db.expire_all()
    assert db.query(OohIntervention).filter_by(dataset_id=ds.id).count() == 2
    new_id = db.query(OohIntervention).filter_by(dataset_id=ds.id, person="New").one().id
    b.post(f"/analysis/datasets/{ds.id}/rows/{new_id}/delete")
    b.post(f"/analysis/datasets/{ds.id}/rename", {"name": "Renamed"})
    db.expire_all()
    assert db.query(OohIntervention).filter_by(dataset_id=ds.id).count() == 1 and db.get(OohDataset, ds.id).name == "Renamed"
    # deleting needs the typed confirmation, then removes the rows too
    b.post(f"/analysis/datasets/{ds.id}/delete", {"confirm": "no"})
    assert db.get(OohDataset, ds.id) is not None
    b.post(f"/analysis/datasets/{ds.id}/delete", {"confirm": "DELETE"})
    db.expire_all()
    assert db.get(OohDataset, dsid) is None and db.query(OohIntervention).filter_by(dataset_id=dsid).count() == 0
    actions = {a.action for a in db.query(AuditLog)}
    assert {"ooh_save", "ooh_rename", "ooh_row_add", "ooh_row_update", "ooh_row_delete", "ooh_delete"} <= actions


def test_only_the_owner_or_an_admin_may_change_a_dataset(browser, db, users):
    from timelog.models import OohDataset
    _save(browser("manager"))
    ds = db.query(OohDataset).one()
    head = browser("head")                                   # a manager too (has reports) but not the owner
    assert head.get("/analysis/datasets/%d" % ds.id).status_code == 200            # can look
    assert head.post(f"/analysis/datasets/{ds.id}/delete", {"confirm": "DELETE"}).status_code == 404
    assert head.post(f"/analysis/datasets/{ds.id}/rename", {"name": "x"}).status_code == 404
    assert browser("alice").get(f"/analysis/datasets/{ds.id}").status_code == 404   # not a manager
    assert browser("admin").post(f"/analysis/datasets/{ds.id}/rename", {"name": "By admin"}).status_code == 303


def test_one_upload_gives_experts_and_officers_panes(browser, users):
    b = browser("manager")
    rows = [("a", "Bob Test", "2026-03-07 10:00:00", "2026-03-07 12:00:00"),          # Expert
            ("b", "Alice Test", "2026-03-07 10:00:00", "2026-03-07 13:00:00"),        # normal employee (Officer)
            ("c", "Unknown Person", "2026-03-07 10:00:00", "2026-03-07 14:00:00")]    # no Timelog user
    page = _upload(b, rows).text
    experts = page.split('id="pane-experts"')[1].split('id="pane-officers"')[0]
    officers = page.split('id="pane-officers"')[1]
    assert "Bob Test" in experts and "Alice Test" not in experts
    assert "Alice Test" in officers and "Bob Test" not in officers
    assert "Unknown Person" in page and "must contain the columns" in page and 'href="#pane-officers"' in page
    assert b.get("/ooh", follow_redirects=False).status_code == 301


def test_name_variants_are_merged_under_the_timelog_name(browser, users):
    b = browser("manager")
    rows = [("a", "Bob Test", "2026-03-07 10:00:00", "2026-03-07 12:00:00"),
            ("b", "test, bob", "2026-03-08 10:00:00", "2026-03-08 11:00:00"),
            ("c", "bob", "2026-03-14 10:00:00", "2026-03-14 11:00:00")]
    page = _upload(b, rows).text
    assert page.count("Bob Test") >= 2 and "test, bob" not in page and ">bob<" not in page
    assert "<strong>4.0</strong>" in page                      # 2 + 1 + 1 h, summed under one name


def test_change_request_number_is_read_listed_saved_and_editable(browser, db, users):
    from timelog.models import OohDataset
    p = ooh.parse_file(_xlsx([("a", "Bob Test", "2026-03-07 10:00:00", "2026-03-07 12:00:00")]), "x.xlsx")
    assert p.items[0].number == "T1"                                     # the "Number" column
    nonum = "Short description,Assigned to,Planned start date,Planned end date\nx,Bob,2026-03-07 10:00:00,2026-03-07 11:00:00\n"
    assert ooh.parse_file(nonum.encode(), "x.csv").items[0].number == ""   # optional column
    b = browser("manager")
    page = _save(b).text
    assert 'class="chart-open"' in page and 'class="table chart-items"' in page            # click a chart: its items
    assert "<td class=\"nowrap\">T1</td>" in page.split('class="chart-items"')[1]
    ds = db.query(OohDataset).one()
    assert ds.interventions[0].number == "T1"
    assert 'name="number"' in b.get(f"/analysis/datasets/{ds.id}").text
    row = ds.interventions[0]
    b.post(f"/analysis/datasets/{ds.id}/rows/{row.id}", {"number": "CHG42", "person": "Bob Test", "description": "d",
                                                         "start": "2026-03-07 10:00:00", "end": "2026-03-07 12:00:00"})
    db.expire_all()
    assert db.get(type(row), row.id).number == "CHG42"
    assert "CHG42" in b.get(f"/analysis/view?ids={ds.id}").text
