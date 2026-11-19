import datetime as dt
from typing import List, Optional

from fastapi import APIRouter, Depends, Form, Request
from sqlalchemy.orm import Session

from .. import calc, workflow
from ..calendar_utils import day_type, fmt_minutes, holiday_names, month_bounds, parse_hhmm, shift_month
from ..config import get_settings
from ..db import get_db
from ..models import KIND_OVERTIME, PAID, OvertimeEntry, User
from ..security import current_user, flash, verify_csrf
from ..web import redirect, render
from .common import check_month, remember_month, remembered_month, resolve_target, sheet_permissions, submission_context

router = APIRouter(prefix="/overtime")
MAX_GRID_ROWS = 100  # 31 days plus extra sessions; each row costs a few queries


def _in_window(start: dt.date, end: dt.date, first: dt.date, last: dt.date) -> bool:
    return end >= first and start <= last


@router.get("")
def current_month(request: Request, me: User = Depends(current_user)):
    year, month = remembered_month(request)
    return redirect(f"/overtime/{year}/{month}")


@router.get("/{year}/{month}")
def view(request: Request, year: int, month: int, user_id: Optional[int] = None,
         me: User = Depends(current_user), db: Session = Depends(get_db)):
    return _page(request, year, month, user_id, me, db)


def _page(request: Request, year: int, month: int, user_id: Optional[int], me: User, db: Session,
          posted: Optional[list] = None):
    """Render the month. `posted` = grid rows as typed by the user after a refused save, shown instead of the saved data."""
    check_month(year, month)
    remember_month(request, year, month)
    s = get_settings()
    target = resolve_target(db, me, user_id)
    if target.is_management:
        flash(request, "Management accounts only approve sheets: there is nothing to declare.", "info")
        return redirect("/")
    start, end = month_bounds(year, month)
    sheets = workflow.get_sheets(db, target, KIND_OVERTIME, year, month)
    blocks = []
    for sheet in sheets:
        perms = sheet_permissions(me, target, sheet)
        blocks.append({"sheet": sheet, "entries": sheet.overtime_entries,
                       "totals": calc.overtime_totals(sheet.overtime_entries), **perms})
    own = me.id == target.id
    first, last = workflow.overtime_window(s)
    # Pre-fill the form with a date of the displayed month (today if it is the current month).
    default_date = min(max(dt.date.today(), start), end)
    default_date = min(max(default_date, first), last)
    latest = sheets[-1] if sheets else None
    # The monthly grid edits the latest sheet when it is a draft / rejected one; after an approval (or with no sheet yet)
    # saving opens a new (supplementary) sheet. While the sheet is submitted the month is read-only.
    can_add = own and not target.is_expert
    grid_sheet = latest if latest is not None and latest.editable else None
    grid_active = can_add and _in_window(start, end, first, last) and (latest is None or latest.status != "submitted")
    window_oncall = set(workflow.oncall_dates(db, target, first, last)) if own else set()
    grid_rows = []
    if grid_active:
        hol = set(holiday_names(db, start, end))
        if posted is not None:
            prev = None
            for r in posted:
                try:
                    day = dt.date.fromisoformat(r["date"])
                except ValueError:
                    continue
                grid_rows.append({**r, "date": day, "first": day != prev, "type": day_type(day, hol),
                                  "on_call": day in window_oncall})
                prev = day
        else:
            by_day = {}
            for e in (grid_sheet.overtime_entries if grid_sheet else []):
                by_day.setdefault(e.date, []).append(e)
            day = max(start, first)
            while day <= min(end, last):
                entries = sorted(by_day.get(day, []), key=lambda e: e.start_minute) or [None]
                for i, e in enumerate(entries):
                    row = {"id": e.id, "start": fmt_minutes(e.start_minute), "end": fmt_minutes(e.end_minute),
                           "comp": e.compensation, "incident": e.incident_ref, "desc": e.description} if e else \
                          {"id": "", "start": "", "end": "", "comp": PAID, "incident": "", "desc": ""}
                    grid_rows.append({**row, "date": day, "first": i == 0, "type": day_type(day, hol),
                                      "on_call": day in window_oncall})
                day += dt.timedelta(days=1)
    for b in blocks:
        b["grid"] = grid_sheet is not None and b["sheet"] is grid_sheet and grid_active
    return render(request, "overtime.html", {
        "year": year, "month": month, "target": target, "own": own, "blocks": blocks,
        "holidays": holiday_names(db, start, end),
        "factors": {code: (f"{float(day):g}", f"{float(night):g}") for code, (day, night) in workflow.til_factors(db).items()},
        "oncall_dates": workflow.oncall_dates(db, target, start, end),
        "grid_active": grid_active, "grid_rows": grid_rows,
        "can_add": can_add,
        "next_is_supplement": latest is not None and latest.status == "approved",
        "latest_submitted": latest is not None and latest.status == "submitted",
        "window_first": first, "window_last": last, "default_date": default_date,
        "step": s.overtime_step_minutes,
        "month_in_window": _in_window(start, end, first, last),
        "prev_month": shift_month(year, month, -1), "next_month": shift_month(year, month, 1),
        **(submission_context(db, me, year, month) if own else {}),
    })


@router.post("/entries", dependencies=[Depends(verify_csrf)])
def add_entry(request: Request,
              date: str = Form(...), start: str = Form(...), end: str = Form(...),
              compensation: str = Form(PAID), incident_ref: str = Form(""), description: str = Form(""),
              me: User = Depends(current_user), db: Session = Depends(get_db)):
    """Add an entry to the sheet of the entry's month: any month within the look-back window."""
    try:
        day = dt.date.fromisoformat(date)
    except ValueError:
        flash(request, "Invalid date.", "error")
        return redirect("/overtime")
    url = f"/overtime/{day.year}/{day.month}"
    try:
        try:
            inp = workflow.OvertimeInput(
                date=day, start_minute=parse_hhmm(start), end_minute=parse_hhmm(end) % 1440,
                compensation=compensation, incident_ref=incident_ref, description=description)
        except ValueError as exc:
            raise workflow.WorkflowError(str(exc))
        sheet = workflow.get_or_create_sheet(db, me, KIND_OVERTIME, day.year, day.month)
        created = workflow.add_overtime(db, get_settings(), sheet, inp)
        db.commit()
    except workflow.WorkflowError as exc:
        db.rollback()
        flash(request, str(exc), "error")
        return redirect(url)
    if sheet.seq > 1 and len(sheet.overtime_entries) == len(created):
        flash(request, "This month was already approved: the entry was added to a new supplementary sheet. "
                       "Submit it so your manager can validate the late claim.", "info")
    elif len(created) > 1:
        flash(request, "Entry crosses midnight: it was split into one entry per calendar day.", "info")
    else:
        flash(request, "Overtime entry added.", "success")
    text, late = workflow.timing_message(db, get_settings(), day.year, day.month)
    if late:
        flash(request, text, "info")
    return redirect(url)


@router.post("/entries/{entry_id}/delete", dependencies=[Depends(verify_csrf)])
def delete_entry(request: Request, entry_id: int, me: User = Depends(current_user), db: Session = Depends(get_db)):
    entry = db.get(OvertimeEntry, entry_id)
    if entry is None or entry.sheet.user_id != me.id:
        flash(request, "Entry not found.", "error")
        return redirect("/overtime")
    sheet = entry.sheet
    try:
        workflow.delete_overtime(db, sheet, entry_id)
        db.commit()
        flash(request, "Entry deleted.", "success")
    except workflow.WorkflowError as exc:
        db.rollback()
        flash(request, str(exc), "error")
    return redirect(f"/overtime/{sheet.year}/{sheet.month}")


@router.post("/{year}/{month}/save", dependencies=[Depends(verify_csrf)])
def save_grid(request: Request, year: int, month: int,
              row_id: List[str] = Form(default=[]), date: List[str] = Form(default=[]),
              start: List[str] = Form(default=[]), end: List[str] = Form(default=[]),
              compensation: List[str] = Form(default=[]), incident_ref: List[str] = Form(default=[]),
              description: List[str] = Form(default=[]),
              me: User = Depends(current_user), db: Session = Depends(get_db)):
    """Save the monthly grid: one row per overtime session (blank rows are ignored, a cleared row deletes its entry)."""
    check_month(year, month)
    url = f"/overtime/{year}/{month}"
    n = len(date)
    if n > MAX_GRID_ROWS:
        flash(request, f"Too many rows (maximum {MAX_GRID_ROWS}): nothing was saved.", "error")
        return redirect(url)
    if not all(len(x) == n for x in (row_id, start, end, compensation, incident_ref, description)):
        flash(request, "The form was incomplete: nothing was saved.", "error")
        return redirect(url)
    rows = []
    posted = [{"id": row_id[i].strip(), "date": date[i], "start": start[i], "end": end[i], "comp": compensation[i] or PAID,
               "incident": incident_ref[i], "desc": description[i]} for i in range(n)]
    try:
        for i in range(n):
            label = date[i][:10]  # user input echoed in messages is cut: flashes live in the session cookie
            entry_id = int(row_id[i]) if row_id[i].strip().isdigit() else None
            if not start[i].strip() and not end[i].strip():
                if incident_ref[i].strip() or description[i].strip():
                    raise workflow.WorkflowError(f"{label}: enter the From and To times, or clear the row.")
                rows.append((entry_id, None))
                continue
            try:
                day = dt.date.fromisoformat(date[i])
                inp = workflow.OvertimeInput(
                    date=day, start_minute=parse_hhmm(start[i]), end_minute=parse_hhmm(end[i]) % 1440,
                    compensation=compensation[i] or PAID, incident_ref=incident_ref[i], description=description[i])
            except ValueError as exc:
                raise workflow.WorkflowError(f"{label}: {str(exc)[:120]}")
            rows.append((entry_id, inp))
        if not any(r[0] or r[1] for r in rows):
            flash(request, "Nothing to save.", "info")
            return redirect(url)
        sheet = workflow.get_sheet(db, me, KIND_OVERTIME, year, month)
        opened_supplement = sheet is not None and sheet.status == "approved"
        if sheet is None or opened_supplement:
            rows = [r for r in rows if r[1] is not None]  # nothing to delete on a sheet that does not exist yet
            if not rows:
                flash(request, "Nothing to save.", "info")
                return redirect(url)
            sheet = workflow.get_or_create_sheet(db, me, KIND_OVERTIME, year, month)
        added, deleted = workflow.save_overtime_rows(db, get_settings(), sheet, rows)
        db.commit()
    except workflow.WorkflowError as exc:
        db.rollback()
        flash(request, f"Nothing was saved. {exc}", "error")
        return _page(request, year, month, None, me, db, posted=posted)  # keep what the user typed
    if opened_supplement:
        flash(request, "This month was already approved: the entries were added to a new supplementary sheet. "
                       "Submit it so your manager can validate the late claim.", "info")
    else:
        flash(request, "Overtime saved.", "success")
    if added:
        text, late = workflow.timing_message(db, get_settings(), year, month)
        if late:
            flash(request, text, "info")
    return redirect(url)
