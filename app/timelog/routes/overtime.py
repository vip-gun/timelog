import datetime as dt
from typing import Optional

from fastapi import APIRouter, Depends, Form, Request
from sqlalchemy.orm import Session

from .. import calc, workflow
from ..calendar_utils import holiday_names, month_bounds, parse_hhmm, shift_month
from ..config import get_settings
from ..db import get_db
from ..models import KIND_ONCALL, KIND_OVERTIME, PAID, OvertimeEntry, User
from ..security import current_user, flash, verify_csrf
from ..web import redirect, render
from .common import check_month, remember_month, remembered_month, resolve_target, sheet_permissions, submission_context

router = APIRouter(prefix="/overtime")


@router.get("")
def current_month(request: Request, me: User = Depends(current_user)):
    year, month = remembered_month(request)
    return redirect(f"/overtime/{year}/{month}")


@router.get("/{year}/{month}")
def view(request: Request, year: int, month: int, user_id: Optional[int] = None,
         me: User = Depends(current_user), db: Session = Depends(get_db)):
    check_month(year, month)
    remember_month(request, year, month)
    s = get_settings()
    target = resolve_target(db, me, user_id)
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
    return render(request, "overtime.html", {
        "year": year, "month": month, "target": target, "own": own, "blocks": blocks,
        "holidays": holiday_names(db, start, end),
        "oncall_dates": workflow.oncall_dates(db, target, start, end),
        # on-call days within the declarable window, for the "on-call day detected" hint of the form
        "window_oncall": sorted(d.isoformat() for d in workflow.oncall_dates(db, target, first, last)) if own else [],
        "can_add": own and not target.is_expert,
        "next_is_supplement": latest is not None and latest.status == "approved",
        "latest_submitted": latest is not None and latest.status == "submitted",
        "window_first": first, "window_last": last, "default_date": default_date,
        "step": s.overtime_step_minutes,
        "month_in_window": end >= first and start <= last,
        "prev_month": shift_month(year, month, -1), "next_month": shift_month(year, month, 1),
        **(submission_context(db, me, year, month) if own else {}),
    })


@router.post("/entries", dependencies=[Depends(verify_csrf)])
def add_entry(request: Request,
              date: str = Form(...), start: str = Form(...), end: str = Form(...),
              compensation: str = Form(PAID), preapproved: Optional[str] = Form(None), incident_ref: str = Form(""), description: str = Form(""),
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
                compensation=compensation, preapproved=bool(preapproved), incident_ref=incident_ref, description=description)
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
