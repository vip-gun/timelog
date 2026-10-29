import datetime as dt
from decimal import Decimal
from typing import Dict, List, Optional

from fastapi import APIRouter, BackgroundTasks, Depends, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import workflow
from ..app_settings import can_see_amounts
from ..calendar_utils import (
    day_type, holiday_names, month_bounds, month_days, month_label, rotation_week, rotation_week_start,
)
from ..config import get_settings
from ..db import get_db
from ..models import KIND_ONCALL, KIND_OVERTIME, OvertimeEntry, Sheet, User
from ..security import current_user, flash, verify_csrf
from ..web import redirect, render
from .common import check_month, remember_month, remembered_month, resolve_target, sheet_permissions, signature_from_form, submission_context
from .notifications import notify_submitted
from .submission import save_signature_picture, submitted_message

router = APIRouter(prefix="/oncall")
WEEKDAY_NAMES = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]


@router.get("")
def current_month(request: Request, me: User = Depends(current_user)):
    year, month = remembered_month(request)
    return redirect(f"/oncall/{year}/{month}")


@router.get("/{year}/{month}")
def view(request: Request, year: int, month: int, user_id: Optional[int] = None,
         me: User = Depends(current_user), db: Session = Depends(get_db)):
    check_month(year, month)
    remember_month(request, year, month)
    s = get_settings()
    target = resolve_target(db, me, user_id)
    sheet = workflow.get_sheet(db, target, KIND_ONCALL, year, month)
    start, end = month_bounds(year, month)
    names = holiday_names(db, start, end)
    holidays = set(names)
    by_date = {d.date: d for d in sheet.oncall_days} if sheet else {}
    is_expert = sheet.snapshot_is_expert if sheet and not sheet.editable else target.is_expert

    # Non-experts: interventions are the overtime hours logged on on-call days (read-only here).
    ot_hours: Dict[dt.date, Decimal] = {}
    if not is_expert:
        for date, hours in db.execute(
                select(OvertimeEntry.date, OvertimeEntry.hours).join(Sheet).where(
                    Sheet.user_id == target.id, Sheet.kind == KIND_OVERTIME, Sheet.year == year, Sheet.month == month)):
            ot_hours[date] = ot_hours.get(date, Decimal(0)) + hours

    # Rotation weeks (Friday -> Thursday by default), possibly straddling the month boundary.
    days = month_days(year, month)
    neighbour_dates = workflow.oncall_dates(db, target, start - dt.timedelta(days=6), end + dt.timedelta(days=6))
    week_rows: Dict[dt.date, int] = {}
    for date in days:
        ws = rotation_week_start(date, s.oncall_week_start)
        week_rows[ws] = week_rows.get(ws, 0) + 1
    rows = []
    seen_weeks = set()
    for date in days:
        row = by_date.get(date)
        ws = rotation_week_start(date, s.oncall_week_start)
        week = None
        if ws not in seen_weeks:
            seen_weeks.add(ws)
            week_days = rotation_week(ws)
            outside = [d for d in week_days if not (start <= d <= end)]
            week = {
                "start": ws, "end": week_days[-1], "rowspan": week_rows[ws],
                "on": all(d in neighbour_dates for d in week_days),
                "spill": outside,
                "spill_label": f"{outside[0]:%d/%m}–{outside[-1]:%d/%m}" if outside else "",
                "spill_dir": "previous" if outside and outside[0] < start else "next",
            }
        rows.append({
            "date": date, "day_type": day_type(date, holidays), "holiday": names.get(date, ""), "row": row,
            "week_key": ws.isoformat(), "week": week,
            "ot_hours": ot_hours.get(date) if row else None,
        })
    grade = workflow.grade_of(db, target)
    counts = {}
    for d in (sheet.oncall_days if sheet else []):
        counts[d.day_type] = counts.get(d.day_type, 0) + 1
    return render(request, "oncall.html", {
        "year": year, "month": month, "target": target, "sheet": sheet, "rows": rows, "grade": grade,
        "is_expert": is_expert, "total": workflow.oncall_total(sheet) if sheet else 0, "counts": counts,
        "interventions": (sum(1 for d in (sheet.oncall_days if sheet else []) if d.intervention) if is_expert
                          else sum(1 for r in rows if r["ot_hours"])),
        "show_amounts": can_see_amounts(db, me, target),
        "week_start_name": WEEKDAY_NAMES[s.oncall_week_start],
        "week_end_name": WEEKDAY_NAMES[(s.oncall_week_start + 6) % 7],
        **sheet_permissions(me, target, sheet),
        **(submission_context(db, me, year, month) if me.id == target.id else {}),
    })


@router.post("/{year}/{month}", dependencies=[Depends(verify_csrf)])
async def save(request: Request, year: int, month: int, background: BackgroundTasks,
               me: User = Depends(current_user), db: Session = Depends(get_db)):
    check_month(year, month)
    s = get_settings()
    form = await request.form()
    start, end = month_bounds(year, month)

    # Days ticked individually + every day of the rotation weeks toggled on.
    wanted = {date for date in month_days(year, month) if form.get(f"oc_{date.isoformat()}")}
    spill: Dict[tuple, List[dt.date]] = {}
    for key in form.keys():
        if not key.startswith("wk_") or not form.get(key):
            continue
        try:
            ws = dt.date.fromisoformat(key[3:])
        except ValueError:
            continue
        if rotation_week_start(ws, s.oncall_week_start) != ws:
            continue
        for d in rotation_week(ws):
            if start <= d <= end:
                wanted.add(d)
            else:
                spill.setdefault((d.year, d.month), []).append(d)

    days = [workflow.OnCallInput(
        date=date, intervention=bool(form.get(f"iv_{date.isoformat()}")),
        comment=str(form.get(f"cm_{date.isoformat()}", "")), incident_ref=str(form.get(f"in_{date.isoformat()}", "")),
    ) for date in sorted(wanted)]
    action = form.get("action", "save")
    url = f"/oncall/{year}/{month}"
    try:
        sheet = workflow.get_or_create_sheet(db, me, KIND_ONCALL, year, month)
        workflow.save_oncall(db, sheet, days)
        spilled, blocked = [], []
        for (y, m), dates in sorted(spill.items()):
            other = workflow.get_or_create_sheet(db, me, KIND_ONCALL, y, m)
            try:
                if workflow.add_oncall_days(db, other, dates):
                    spilled.append(month_label(y, m))
            except workflow.WorkflowError:
                blocked.append(month_label(y, m))
        db.commit()
        if action == "submit":
            signature = signature_from_form(request, form, db)
            submitted = workflow.submit_month(db, s, me, year, month, str(form.get("employee_comment", "")), signature)
            if signature is not None and form.get("save_signature"):
                save_signature_picture(db, me, signature.png)
            db.commit()
    except workflow.WorkflowError as exc:
        db.rollback()
        flash(request, str(exc), "error")
        return redirect(url)
    if spilled:
        flash(request, f"Rotation week continues into {', '.join(spilled)}: those days were added to that "
                       "month's on-call sheet (draft).", "info")
    if blocked:
        flash(request, f"The {', '.join(blocked)} sheet is submitted or approved: the days of the rotation week "
                       "falling in that month were not added.", "error")
    if action == "submit":
        flash(request, submitted_message(submitted), "success")
        for done in submitted:
            notify_submitted(background, done)
    else:
        flash(request, "On-call sheet saved (draft).", "success")
    return redirect(url)
