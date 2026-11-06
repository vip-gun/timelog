"""Finance exports in the layout of the two reference workbooks (on-call sheet and overtime sheet).

The layouts live in `export_templates/` (formats, widths, merged cells, conditional formats, fixed headings; no personal
data). This module copies their styles and writes the values: the cells hold the figures the application computed
(on-call amounts are the rates snapshotted at submission), not spreadsheet formulas. A holiday that falls on a Saturday
or Sunday counts once, as a holiday.

Each export contains the tab of the payroll month (sheets whose work month is the payroll month) and, when late claims
are paid in that period, one extra tab per earlier work month, named "<tab> (late)".
"""
import calendar
import datetime as dt
import io
from collections import defaultdict
from copy import copy
from decimal import Decimal
from pathlib import Path
from typing import Dict, List, Tuple

from openpyxl import load_workbook
from openpyxl.cell.rich_text import CellRichText, TextBlock
from openpyxl.cell.text import InlineFont
from openpyxl.formatting.rule import Rule
from openpyxl.worksheet.datavalidation import DataValidation
from sqlalchemy.orm import Session

from .calendar_utils import day_type, holiday_names, month_days
from .export import payroll_sheets, scope_users
from .models import (
    DAY_TYPES, HOLIDAY, KIND_ONCALL, KIND_OVERTIME, SATURDAY, SUNDAY, TIL, WEEKDAY, Sheet, User,
)
from .workflow import default_grade

TEMPLATE_DIR = Path(__file__).parent / "export_templates"
DAY_COLS = 31          # D..AH hold up to 31 days
FIRST_DAY_COL = 4      # column D
# overtime weekday headings of the reference workbook
OT_DAY_LABEL = ["Mo", "Tu", "We", "Th", "Fr", "Sa", "Sun"]
ONCALL_TYPE = {WEEKDAY: "W", SATURDAY: "Sat", SUNDAY: "Sun", HOLIDAY: "H"}


def _tab_name(year: int, month: int, with_year: bool) -> str:
    abbr = "SEPT" if month == 9 and with_year else calendar.month_abbr[month].upper()
    return f"{abbr} {year}" if with_year else abbr


def _full_name(u: User) -> str:
    return f"{u.first_name} {u.last_name}".strip() or u.username


def _snap(ws, row: int, first_col: int, last_col: int) -> List:
    """Copy of the cell formats of one template row."""
    return [copy(ws.cell(row, c)._style) for c in range(first_col, last_col + 1)]


def _put_row_style(ws, row: int, style: List, first_col: int = 1) -> None:
    for offset, st in enumerate(style):
        ws.cell(row, first_col + offset)._style = copy(st)


def _group_by_work_month(sheets: List[Sheet], payroll_month: Tuple[int, int]) -> Dict[Tuple[int, int], List[Sheet]]:
    groups: Dict[Tuple[int, int], List[Sheet]] = defaultdict(list)
    for s in sheets:
        groups[(s.year, s.month)].append(s)
    groups.setdefault(payroll_month, [])
    return dict(sorted(groups.items()))


def _scope_sheets(db: Session, viewer: User, year: int, month: int, team: str, kind: str):
    users = scope_users(db, viewer, team)
    sheets = [s for s in payroll_sheets(db, users, year, month) if s.kind == kind]
    return users, _group_by_work_month(sheets, (year, month))


def _xlsx(wb) -> bytes:
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


# ------------------------------------------------------------------ on-call
def _oncall_rules(ws) -> List[Rule]:
    """The month / holiday / Saturday / Sunday shading rules of the template (the check-cell rules are dropped)."""
    rules, seen = [], set()
    for _, rs in ws.conditional_formatting._cf_rules.items():
        for r in rs:
            f = r.formula[0] if r.formula else ""
            if f.startswith("$A$121") or f in seen:
                continue
            seen.add(f)
            rules.append(r)
    return rules


def _fill_oncall_tab(ws, rules, db: Session, year: int, month: int, sheets: List[Sheet], holidays: set,
                     default_rates: Dict[str, Decimal]) -> None:
    protos = {k: _snap(ws, r, 1, 39) for k, r in dict(
        team=7, emp=8, inc=10, ptitle=60, p62=62, p63=63, p64=64, p65=65, p66=66, pteam=67, pemp=68, pinc=70,
        pgap=74, pclose=119).items()}
    h4, h62 = ws.row_dimensions[4].height, ws.row_dimensions[62].height
    ws.delete_rows(6, 400)
    for r in [r for r in ws.row_dimensions if r >= 6]:
        del ws.row_dimensions[r]

    first = dt.date(year, month, 1)
    dates = [first + dt.timedelta(days=i) for i in range(DAY_COLS)]   # the template runs on into the next month
    dtypes = [day_type(d, holidays) for d in dates]
    types = [ONCALL_TYPE[t] for t in dtypes]
    for i, d in enumerate(dates):
        c = FIRST_DAY_COL + i
        ws.cell(3, c).value = d
        ws.cell(4, c).value = d.strftime("%a")
        ws.cell(5, c).value = types[i]
    ws["B5"].value = first

    # employees by team, in the order of the reference sheet: team header, employees (Experts get an incidents row)
    by_team: Dict[str, List[Sheet]] = defaultdict(list)
    for s in sheets:
        by_team[s.user.team or "Other"].append(s)
    row = 7
    layout = []   # (row, kind, sheet or None, label)
    for team in sorted(by_team):
        layout.append((row, "team", None, team))
        row += 1
        for s in sorted(by_team[team], key=lambda s: s.user.sort_key):
            layout.append((row, "emp", s, _full_name(s.user)))
            row += 1
            if s.snapshot_is_expert:
                layout.append((row, "inc", s, _full_name(s.user) + " - Incidents/calls"))
                row += 1
        layout.append((row, "gap", None, team))   # blank line between teams (the pricing block shows the team subtotal)
        row += 1
    last_emp = max([r for r, k, _, _ in layout if k not in ("team", "gap")], default=7)
    title = last_emp + 3
    shift = title                       # pricing row = employee row + shift (68 = 8 + 60)

    def day_index(d: dt.date) -> int:
        return (d - first).days

    team_hdr, team_sum, grand = None, Decimal(0), Decimal(0)
    for r, kind, s, label in layout:
        pr = r + shift
        if kind == "gap":
            # pricing block: the team subtotal closes the team (AM, double rule); the team header row carries it in AI too
            _put_row_style(ws, pr, protos["pgap"])
            ws.cell(pr, 39).value = float(team_sum)                     # AM: team subtotal of the payroll check
            ws.cell(team_hdr + shift, 35).value = float(team_sum)       # AI of the team header row
            grand += team_sum
            continue
        _put_row_style(ws, r, protos[kind])
        ws.cell(r, 2).value = label
        _put_row_style(ws, pr, protos["p" + kind])
        ws.cell(pr, 2).value = label
        if kind == "team":
            team_hdr, team_sum = r, Decimal(0)
            continue
        total, total_amount = 0, Decimal(0)
        for d in s.oncall_days:
            idx = day_index(d.date)
            if not 0 <= idx < DAY_COLS:
                continue
            if kind == "inc" and not d.intervention:
                continue
            c = FIRST_DAY_COL + idx
            ws.cell(r, c).value = 1
            ws.cell(pr, c).value = float(d.rate)
            total += 1
            total_amount += Decimal(d.rate)
        ws.cell(r, 35).value = total                 # AI: number of days
        ws.cell(pr, 35).value = float(total_amount)  # AI: amount
        ws.cell(pr, 37).value = label                # AK..AL: the "Addition for Payroll's Check" column
        ws.cell(pr, 38).value = float(total_amount)
        team_sum += total_amount

    # pricing sheet header
    _put_row_style(ws, title, protos["ptitle"])
    base = dict(rFont="Georgia", sz=12)
    ws.cell(title, 4).value = CellRichText(
        TextBlock(InlineFont(**base), "PRICING SHEET ("),
        TextBlock(InlineFont(color="FFFF0000", **base), "Automatic - no input please"),
        TextBlock(InlineFont(**base), ")  -  ON CALL PRIME request"))
    for off, key in ((2, "p62"), (3, "p63"), (4, "p64"), (5, "p65"), (6, "p66")):
        _put_row_style(ws, title + off, protos[key])
    ws.row_dimensions[title + 2].height = h62
    ws.cell(title + 3, 3).value = "Date"
    ws.cell(title + 4, 3).value = "Price"
    ws.cell(title + 2, 37).value = first                  # AK: month label ("June 26")
    ws.cell(title + 2, 38).value = "Addition for Payroll's"
    ws.cell(title + 3, 38).value = "Check"
    for i, d in enumerate(dates):
        c = FIRST_DAY_COL + i
        ws.cell(title + 2, c).value = d.strftime("%a")
        ws.cell(title + 3, c).value = d
        ws.cell(title + 4, c).value = float(default_rates[dtypes[i]])
    close = max([r for r, k, _, _ in layout if k == "gap"], default=last_emp + 1) + shift + 1
    _put_row_style(ws, close, protos["pclose"])
    ws.cell(close, 38).value = float(grand)               # AL: total of the check column
    ws.row_dimensions[4].height = h4

    # shading rules over the new blocks
    blocks = ["D3:AH5", f"D7:AH{last_emp}", f"D{title + 2}:AH{title + 4}", f"D{title + 7}:AH{last_emp + shift}"]
    ws.conditional_formatting = type(ws.conditional_formatting)()
    for rule in rules:
        ws.conditional_formatting.add(" ".join(blocks), copy(rule))
    ws.freeze_panes = "D7"


def build_oncall_export(db: Session, viewer: User, year: int, month: int, team: str = "") -> bytes:
    _, groups = _scope_sheets(db, viewer, year, month, team, KIND_ONCALL)
    wb = load_workbook(TEMPLATE_DIR / "oncall_layout.xlsx")
    proto = wb["MONTH"]
    rules = _oncall_rules(proto)
    grade = default_grade(db)
    rates = {WEEKDAY: grade.rate_weekday, SATURDAY: grade.rate_saturday, SUNDAY: grade.rate_sunday,
             HOLIDAY: grade.rate_holiday}
    holiday_dates = holiday_names(db, dt.date(year - 1, 1, 1), dt.date(year + 1, 12, 31))
    holidays = set(holiday_dates)

    tabs = []
    for (wy, wm), sheets in groups.items():
        ws = wb.copy_worksheet(proto)
        ws.title = _tab_name(wy, wm, False) + ("" if (wy, wm) == (year, month) else " (late)")
        _fill_oncall_tab(ws, rules, db, wy, wm, sheets, holidays, rates)
        tabs.append(ws)
    wb.remove(proto)
    wb.move_sheet("Support", offset=len(wb.sheetnames))
    sup = wb["Support"]
    sup["C5"].value = year
    prices = [rates[WEEKDAY]] * 5 + [rates[SATURDAY], rates[SUNDAY], rates[HOLIDAY]]
    for i, p in enumerate(prices):
        sup.cell(47 + i, 4).value = float(p)
    for i, (d, name) in enumerate(sorted(h for h in holiday_dates.items() if h[0].year in (year, year + 1))[:15]):
        sup.cell(27 + i, 2).value, sup.cell(27 + i, 3).value, sup.cell(27 + i, 4).value = d, d.strftime("%a"), name
    wb.active = 0
    return _xlsx(wb)


# ------------------------------------------------------------------ overtime
def _fill_overtime_tab(ws, db: Session, year: int, month: int, sheets: List[Sheet], holidays: set) -> None:
    first_row = _snap(ws, 4, 1, 52)
    mid_row = _snap(ws, 5, 1, 52)
    last_row = _snap(ws, 19, 1, 52)
    note_style = {(r, c): copy(ws.cell(r, c)._style) for r in range(22, 26) for c in range(5, 32)}
    note_text = "NOTE FOR NON-RESIDENTS: Working during the week-end will impact the homeworking balance"
    for rng in [str(m) for m in ws.merged_cells.ranges if m.min_row >= 4]:
        ws.unmerge_cells(rng)
    ws.delete_rows(4, 200)
    for r in [r for r in ws.row_dimensions if r >= 4]:
        del ws.row_dimensions[r]

    ws["A2"].value = dt.date(year, month, 1)
    days = month_days(year, month)
    for i in range(DAY_COLS):
        c = FIRST_DAY_COL + i
        if i < len(days):
            d = days[i]
            label = OT_DAY_LABEL[d.weekday()] + ("\n LH" if d in holidays else "")
            ws.cell(2, c).value, ws.cell(3, c).value = d, label
        else:
            ws.cell(2, c).value = ws.cell(3, c).value = None

    # one line per employee and compensation: hours per day, night hours per day type, then the payroll columns
    lines: Dict[Tuple[int, str], dict] = {}
    for s in sheets:
        for e in s.overtime_entries:
            ln = lines.setdefault((s.user_id, e.compensation), {
                "user": s.user, "comp": e.compensation, "days": defaultdict(Decimal),
                "hours": {t: Decimal(0) for t in DAY_TYPES}, "night": {t: Decimal(0) for t in DAY_TYPES},
                "credit": Decimal(0)})
            ln["days"][e.date.day] += Decimal(e.hours)
            ln["hours"][e.day_type] += Decimal(e.hours)
            ln["night"][e.day_type] += Decimal(e.night_hours)
            ln["credit"] += Decimal(e.til_credit)
    ordered = sorted(lines.values(), key=lambda ln: (ln["user"].sort_key, ln["comp"] != "PAID"))
    r = 4
    for n, ln in enumerate(ordered):
        style = first_row if n == 0 else (last_row if n == len(ordered) - 1 else mid_row)
        _put_row_style(ws, r, style)
        u = ln["user"]
        ws.cell(r, 1).value, ws.cell(r, 2).value = u.last_name or u.username, u.first_name
        ws.cell(r, 3).value = "TIL" if ln["comp"] == TIL else "Paid"
        for day, h in ln["days"].items():
            ws.cell(r, FIRST_DAY_COL + day - 1).value = float(h)
        h, nt = ln["hours"], ln["night"]
        til = ln["comp"] == TIL
        total = sum(h.values(), Decimal(0))
        night_total = sum(nt.values(), Decimal(0))
        values = {
            35: h[WEEKDAY], 36: h[SATURDAY], 37: h[SUNDAY], 38: h[HOLIDAY], 39: total,                  # AI..AM
            40: nt[WEEKDAY], 41: nt[SATURDAY], 42: nt[SUNDAY], 43: nt[HOLIDAY],                          # AN..AQ night hours
            44: ln["credit"] if til else 0,                                                             # AR
            45: 0 if til else total, 46: 0 if til else total, 47: 0 if til else night_total,            # AS, AT, AU
            48: 0 if til else h[SUNDAY], 49: 0 if til else h[HOLIDAY],                                   # AV, AW
            50: ln["credit"] if til else 0,                                                             # AX
        }
        for col, v in values.items():
            ws.cell(r, col).value = float(v)
        r += 1
    last = max(r - 1, 4)
    if lines:
        dv = DataValidation(type="list", formula1="$AY$2:$AZ$2", allow_blank=True)
        ws.add_data_validation(dv)
        dv.add(f"C4:C{last}")
    note = last + 3
    for (tr, c), st in note_style.items():
        ws.cell(note + (tr - 22), c)._style = copy(st)
    ws.cell(note, 5).value = note_text
    ws.merge_cells(start_row=note, start_column=5, end_row=note + 3, end_column=31)
    ws.freeze_panes = "C4"


def build_overtime_export(db: Session, viewer: User, year: int, month: int, team: str = "") -> bytes:
    _, groups = _scope_sheets(db, viewer, year, month, team, KIND_OVERTIME)
    wb = load_workbook(TEMPLATE_DIR / "overtime_layout.xlsx")
    proto = wb["MONTH"]
    holiday_dates = holiday_names(db, dt.date(year - 1, 1, 1), dt.date(year + 1, 12, 31))
    holidays = set(holiday_dates)
    for (wy, wm), sheets in groups.items():
        ws = wb.copy_worksheet(proto)
        ws.title = _tab_name(wy, wm, True) + ("" if (wy, wm) == (year, month) else " (late)")
        _fill_overtime_tab(ws, db, wy, wm, sheets, holidays)
    wb.remove(proto)
    rf = wb["READ FIRST"]
    for i, (d, name) in enumerate(sorted(h for h in holiday_dates.items() if h[0].year in (year, year + 1))[:13]):
        rf.cell(11 + i, 3).value, rf.cell(11 + i, 4).value = d, name
    wb.active = 1 if len(wb.sheetnames) > 1 else 0
    return _xlsx(wb)
