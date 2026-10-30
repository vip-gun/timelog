"""Monthly XLSX export for Finance / Payroll.

The export of period P contains every APPROVED sheet whose payroll period is P (CLAUDE.md §3):
the regular sheets of work month P plus late claims for earlier months approved after their cut-off.
"""
import datetime as dt
import io
from decimal import Decimal
from typing import Dict, List, Optional, Tuple

from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter
from sqlalchemy import and_, or_, select
from sqlalchemy.orm import Session

from . import calc
from .access import subordinate_ids
from .app_settings import get_flag
from .calendar_utils import day_type, fmt_minutes, holiday_set, month_bounds, month_days, month_label
from .models import (
    APPROVED, DAY_TYPE_LABELS, DAY_TYPES, HOLIDAY, KIND_ONCALL, KIND_OVERTIME, SATURDAY, SETTING_TIL_ENABLED,
    SUNDAY, TIL, WEEKDAY, Sheet, User,
)

HEADER_FONT = Font(bold=True, color="FFFFFF")
HEADER_FILL = PatternFill("solid", fgColor="4F4059")
TOTAL_FONT = Font(bold=True)
LATE_FILL = PatternFill("solid", fgColor="F3F1EB")
WEEKEND_FILL = PatternFill("solid", fgColor="ECEBE5")
HOLIDAY_FILL = PatternFill("solid", fgColor="C7B99C")
MONEY = '#,##0.00'
HOURS = '0.00'
SHORT_TYPE = {WEEKDAY: "W", SATURDAY: "Sat", SUNDAY: "Sun", HOLIDAY: "H"}


def scope_users(db: Session, viewer: User, team: str = "") -> List[User]:
    """Users included in the viewer's export: subtree for managers, everyone for admins."""
    if viewer.is_admin or viewer.is_procurement:   # IT Procurement exports the whole organisation
        users = list(db.scalars(select(User)))
    else:
        ids = subordinate_ids(db, viewer)
        users = list(db.scalars(select(User).where(User.id.in_(ids)))) if ids else []
    if team:
        users = [u for u in users if u.team == team]
    return sorted(users, key=lambda u: u.sort_key)


def work_month_sheets(db: Session, users: List[User], year: int, month: int) -> Dict[Tuple[int, str], List[Sheet]]:
    """Sheets of work month (year, month) per (user_id, kind), ordered by seq."""
    out: Dict[Tuple[int, str], List[Sheet]] = {}
    if not users:
        return out
    rows = db.scalars(select(Sheet).where(
        Sheet.user_id.in_([u.id for u in users]), Sheet.year == year, Sheet.month == month).order_by(Sheet.seq))
    for s in rows:
        out.setdefault((s.user_id, s.kind), []).append(s)
    return out


def payroll_sheets(db: Session, users: List[User], year: int, month: int) -> List[Sheet]:
    """Approved sheets paid in payroll period (year, month)."""
    if not users:
        return []
    return list(db.scalars(select(Sheet).where(
        Sheet.user_id.in_([u.id for u in users]), Sheet.status == APPROVED,
        Sheet.payroll_year == year, Sheet.payroll_month == month,
    ).order_by(Sheet.year, Sheet.month, Sheet.seq)))


def late_claims_pending(db: Session, users: List[User], year: int, month: int) -> List[Sheet]:
    """Submitted sheets of earlier work months (will land in a payroll period once approved)."""
    if not users:
        return []
    return list(db.scalars(select(Sheet).where(
        Sheet.user_id.in_([u.id for u in users]), Sheet.status == "submitted",
        or_(Sheet.year < year, and_(Sheet.year == year, Sheet.month < month)),
    )))


def _header(ws, row: int, titles: List[str], widths: Optional[List[int]] = None) -> None:
    for col, title in enumerate(titles, start=1):
        c = ws.cell(row=row, column=col, value=title)
        c.font, c.fill = HEADER_FONT, HEADER_FILL
        c.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
    if widths:
        for col, w in enumerate(widths, start=1):
            ws.column_dimensions[get_column_letter(col)].width = w
    ws.freeze_panes = ws.cell(row=row + 1, column=1)


def _fmt(ws, row: int, cols: range, number_format: str) -> None:
    for col in cols:
        ws.cell(row=row, column=col).number_format = number_format


def _emp_cols(u: User) -> list:
    return [u.team, u.last_name or u.full_name, u.first_name, u.employee_id]


def _work_cols(s: Sheet) -> list:
    label = f"{s.year}-{s.month:02d}"
    return [label, ("Late claim" if s.is_late else "") + (f" (suppl. #{s.seq})" if s.seq > 1 else "")]


def _signed(sheet: Sheet) -> str:
    sig = sheet.signature
    return sig.signed_at.strftime("%d/%m/%Y %H:%M") if sig else ""


def _shade_late(ws, sheet: Sheet, ncols: int) -> None:
    if sheet.is_late:
        for col in range(1, ncols + 1):
            ws.cell(row=ws.max_row, column=col).fill = LATE_FILL


def build_export(db: Session, viewer: User, year: int, month: int, team: str = "") -> bytes:
    users = scope_users(db, viewer, team)
    by_id = {u.id: u for u in users}
    paid = payroll_sheets(db, users, year, month)
    oncall = sorted([s for s in paid if s.kind == KIND_ONCALL], key=lambda s: (by_id[s.user_id].sort_key, s.year, s.month))
    overtime = sorted([s for s in paid if s.kind == KIND_OVERTIME], key=lambda s: (by_id[s.user_id].sort_key, s.year, s.month, s.seq))
    current = work_month_sheets(db, users, year, month)
    # TIL columns only when the feature is on, or when exported sheets still contain TIL entries
    til = get_flag(db, SETTING_TIL_ENABLED) or any(
        e.compensation == TIL for sh in overtime for e in sh.overtime_entries)
    start, end = month_bounds(year, month)
    holidays = holiday_set(db, start, end)
    days = month_days(year, month)

    wb = Workbook()

    # ---------------------------------------------------------------- Summary
    ws = wb.active
    ws.title = "Summary"
    ws["A1"] = f"On-call & Overtime - payroll period {month_label(year, month)}"
    ws["A1"].font = Font(bold=True, size=14, color="4F4059")
    ws["A2"] = f"Generated by {viewer.full_name} on {dt.datetime.now():%d/%m/%Y %H:%M}"
    ws["A3"] = ("Contains APPROVED sheets paid in this period: work done in this month plus late claims "
                "for earlier months (shaded, 'Late claim').")
    if team:
        ws["A4"] = f"Team filter: {team}"
    _header(ws, 6, ["Team", "Last name", "First name", "Employee ID", "Expert",
                    f"On-call {month_label(year, month)}", f"Overtime {month_label(year, month)}"],
            [24, 22, 18, 14, 8, 22, 22])
    for u in users:
        oc = current.get((u.id, KIND_ONCALL), [])
        ot = current.get((u.id, KIND_OVERTIME), [])
        ot_label = "n/a" if u.is_expert else (", ".join(s.status for s in ot) or "-")
        ws.append(_emp_cols(u) + ["Yes" if u.is_expert else "", oc[-1].status if oc else "-", ot_label])
    pending = late_claims_pending(db, users, year, month)
    if pending:
        ws.append([])
        ws.append([f"{len(pending)} late claim(s) for earlier months still awaiting validation - "
                   "they will be included in the period in which they are approved."])
        ws.cell(row=ws.max_row, column=1).font = TOTAL_FONT

    # ---------------------------------------------------------------- On-call summary
    ws = wb.create_sheet("On-Call")
    titles = ["Team", "Last name", "First name", "Employee ID", "Work month", "Note", "Grade", "Expert",
              "Days W", "Days Sat", "Days Sun", "Days H", "Total days", "Expert interventions", "Amount",
              "Signed (UTC)"]
    _header(ws, 1, titles, [24, 22, 18, 14, 11, 20, 8, 8, 9, 9, 9, 9, 10, 13, 12, 17])
    grand = Decimal(0)
    for sh in oncall:
        u = by_id[sh.user_id]
        counts = {t: 0 for t in DAY_TYPES}
        for d in sh.oncall_days:
            counts[d.day_type] += 1
        interventions = sum(1 for d in sh.oncall_days if d.intervention) if sh.snapshot_is_expert else 0
        amount = sum((d.amount for d in sh.oncall_days), Decimal(0))
        grand += amount
        ws.append(_emp_cols(u) + _work_cols(sh) + [
            sh.snapshot_grade_code, "Yes" if sh.snapshot_is_expert else "",
            counts[WEEKDAY], counts[SATURDAY], counts[SUNDAY], counts[HOLIDAY],
            len(sh.oncall_days), interventions, float(amount), _signed(sh)])
        _fmt(ws, ws.max_row, range(15, 16), MONEY)
        _shade_late(ws, sh, len(titles))
    ws.append([])
    ws.append(["TOTAL"] + [""] * 13 + [float(grand)])
    ws.cell(row=ws.max_row, column=1).font = TOTAL_FONT
    ws.cell(row=ws.max_row, column=15).font = TOTAL_FONT
    _fmt(ws, ws.max_row, range(15, 16), MONEY)

    # ---------------------------------------------------------------- On-call grid (legacy layout, work month only)
    ws = wb.create_sheet("On-Call Grid")
    ws.cell(row=1, column=1, value=f"Employee - {month_label(year, month)}").font = TOTAL_FONT
    ws.cell(row=2, column=1, value="Type").font = TOTAL_FONT
    ws.column_dimensions["A"].width = 38
    for i, d in enumerate(days, start=2):
        t = day_type(d, holidays)
        c1 = ws.cell(row=1, column=i, value=d.day)
        c2 = ws.cell(row=2, column=i, value=SHORT_TYPE[t])
        for c in (c1, c2):
            c.alignment = Alignment(horizontal="center")
            c.font = TOTAL_FONT
            if t == HOLIDAY:
                c.fill = HOLIDAY_FILL
            elif t in (SATURDAY, SUNDAY):
                c.fill = WEEKEND_FILL
        ws.column_dimensions[get_column_letter(i)].width = 4.5
    total_col = len(days) + 2
    ws.cell(row=1, column=total_col, value="Total").font = TOTAL_FONT
    ws.freeze_panes = "B3"
    row = 3
    for sh in (s for s in oncall if (s.year, s.month) == (year, month)):
        u = by_id[sh.user_id]
        by_date = {d.date: d for d in sh.oncall_days}
        lines = [(u.full_name, lambda d: d is not None)]
        if sh.snapshot_is_expert:
            lines.append((f"{u.full_name} - Incidents/calls", lambda d: d is not None and d.intervention))
        for label, pred in lines:
            ws.cell(row=row, column=1, value=label)
            total = 0
            for i, day in enumerate(days, start=2):
                if pred(by_date.get(day)):
                    ws.cell(row=row, column=i, value=1).alignment = Alignment(horizontal="center")
                    total += 1
            ws.cell(row=row, column=total_col, value=total)
            row += 1

    # ---------------------------------------------------------------- Overtime payroll
    ws = wb.create_sheet("Overtime Payroll")
    titles = ["Team", "Last name", "First name", "Employee ID", "Work month", "Note",
              "Hours W", "Hours Sat", "Hours Sun", "Hours H", "Total hours", "Night hours",
              "HL100 Normal", "HL101 Normal 50%", "HL111 Night 30%", "HL130 Sunday 70%", "HL121 Legal 200%"]
    if til:
        titles += ["TIL hours worked", "TIL credit (no payment)"]
    n_values = len(titles) - 6
    titles.append("Signed (UTC)")
    _header(ws, 1, titles, [24, 22, 18, 14, 11, 20] + [10] * n_values + [17])
    last_value_col = 6 + n_values
    sums = [Decimal(0)] * n_values
    for sh in overtime:
        u = by_id[sh.user_id]
        t = calc.overtime_totals(sh.overtime_entries)
        values = [t.hours_by_type[WEEKDAY], t.hours_by_type[SATURDAY], t.hours_by_type[SUNDAY],
                  t.hours_by_type[HOLIDAY], t.total_hours, t.total_night,
                  t.hl100, t.hl101, t.hl111, t.hl130, t.hl121] + ([t.til_hours, t.til_credit] if til else [])
        sums = [a + b for a, b in zip(sums, values)]
        ws.append(_emp_cols(u) + _work_cols(sh) + [float(v) for v in values] + [_signed(sh)])
        _fmt(ws, ws.max_row, range(7, last_value_col + 1), HOURS)
        _shade_late(ws, sh, len(titles))
    ws.append([])
    ws.append(["TOTAL", "", "", "", "", ""] + [float(v) for v in sums])
    for col in range(1, last_value_col + 1):
        ws.cell(row=ws.max_row, column=col).font = TOTAL_FONT
    _fmt(ws, ws.max_row, range(7, last_value_col + 1), HOURS)

    # ---------------------------------------------------------------- Overtime detail
    ws = wb.create_sheet("Overtime Detail")
    titles = ["Team", "Last name", "First name", "Employee ID", "Work month", "Note", "Date", "Day type",
              "Start", "End", "Hours", "Night hours"] + (["Compensation", "TIL credit"] if til else []) + [
              "On-call intervention", "Pre-approved", "Incident", "Description"]
    _header(ws, 1, titles, [24, 22, 18, 14, 11, 20, 12, 14, 8, 8, 8, 8] + ([13, 10] if til else []) + [12, 12, 16, 60])
    for sh in overtime:
        u = by_id[sh.user_id]
        for e in sh.overtime_entries:
            ws.append(_emp_cols(u) + _work_cols(sh) + [
                e.date, DAY_TYPE_LABELS[e.day_type], fmt_minutes(e.start_minute), fmt_minutes(e.end_minute),
                float(e.hours), float(e.night_hours)] + ([e.compensation, float(e.til_credit)] if til else []) + [
                "Yes" if e.on_call_intervention else "", "Yes" if e.preapproved else "",
                e.incident_ref, e.description,
            ])
            ws.cell(row=ws.max_row, column=7).number_format = "DD/MM/YYYY"
            _fmt(ws, ws.max_row, range(11, 13), HOURS)
            if til:
                _fmt(ws, ws.max_row, range(14, 15), HOURS)
            _shade_late(ws, sh, len(titles))

    # ---------------------------------------------------------------- On-call detail
    ws = wb.create_sheet("On-Call Detail")
    titles = ["Team", "Last name", "First name", "Employee ID", "Work month", "Note", "Date", "Day type",
              "Intervention", "Rate", "Amount", "Incident", "Comment"]
    _header(ws, 1, titles, [24, 22, 18, 14, 11, 20, 12, 14, 12, 9, 10, 16, 60])
    for sh in oncall:
        u = by_id[sh.user_id]
        for d in sh.oncall_days:
            ws.append(_emp_cols(u) + _work_cols(sh) + [
                d.date, DAY_TYPE_LABELS[d.day_type], "Yes" if d.intervention else "",
                float(d.rate), float(d.amount), d.incident_ref, d.comment])
            ws.cell(row=ws.max_row, column=7).number_format = "DD/MM/YYYY"
            _fmt(ws, ws.max_row, range(10, 12), MONEY)
            _shade_late(ws, sh, len(titles))

    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()
