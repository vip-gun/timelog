"""IT Procurement: payroll reports for Finance and the record that they were sent (closes the payroll period)."""
from typing import Optional

from fastapi import APIRouter, BackgroundTasks, Depends, Form, Request
from fastapi.responses import Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import calc, workflow
from ..app_settings import payroll_cutoff
from ..calendar_utils import month_label, shift_month
from ..config import get_settings
from ..db import get_db
from ..export import build_export
from ..export_finance import build_oncall_export, build_overtime_export
from ..models import KIND_ONCALL, KIND_OVERTIME, SUBMITTED, PayrollRun, Sheet, User
from ..security import flash, payroll_user, verify_csrf
from ..web import redirect, render
from .common import check_month
from .notifications import notify_payroll_sent

router = APIRouter(prefix="/payroll")


def _summary(sheet: Sheet) -> str:
    if sheet.kind == KIND_ONCALL:
        return f"{len(sheet.oncall_days)} day(s)"
    return f"{calc.overtime_totals(sheet.overtime_entries).total_hours:.2f} h"


@router.get("")
def page(request: Request, year: Optional[int] = None, month: Optional[int] = None,
         me: User = Depends(payroll_user), db: Session = Depends(get_db)):
    s = get_settings()
    if year is None or month is None:
        year, month = workflow.default_payroll_period(db, s)
    check_month(year, month)
    closed = workflow.closed_periods(db)
    run = db.scalar(select(PayrollRun).where(PayrollRun.year == year, PayrollRun.month == month))
    sheets = workflow.period_sheets(db, year, month)
    regular = [sh for sh in sheets if (sh.year, sh.month) == (year, month)]
    late = [sh for sh in sheets if (sh.year, sh.month) != (year, month)]
    waiting = list(db.scalars(select(Sheet).where(
        Sheet.status == SUBMITTED, (Sheet.year * 12 + Sheet.month) <= year * 12 + month).order_by(Sheet.year, Sheet.month)))
    downloads, ready = {}, bool(sheets)
    for kind in ("oncall", "overtime"):
        current = workflow.period_digest([sh for sh in sheets if sh.kind == (KIND_ONCALL if kind == "oncall" else KIND_OVERTIME)])
        last = workflow.last_export(db, me, kind, year, month)
        downloads[kind] = {"at": last[0] if last else None, "current": bool(last and last[1] == current)}
        ready = ready and downloads[kind]["current"]
    overdue = [(y, m) for y, m in db.execute(
        select(Sheet.payroll_year, Sheet.payroll_month).where(Sheet.status == "approved", Sheet.payroll_year.is_not(None)).distinct())
        if (y, m) not in closed and (y, m) < (year, month)]
    cutoff = payroll_cutoff(db, s)
    return render(request, "payroll.html", {
        "year": year, "month": month, "run": run, "closed": (year, month) in closed, "regular": regular, "late": late,
        "waiting": waiting, "downloads": downloads, "can_send": ready and run is None, "summary": _summary,
        "overdue": sorted(overdue), "prev": shift_month(year, month, -1), "next": shift_month(year, month, 1),
        "runs": list(db.scalars(select(PayrollRun).order_by(PayrollRun.year.desc(), PayrollRun.month.desc()).limit(12))),
        "deadline": workflow.approval_deadline(db, s, year, month),
        "counts": {"oncall": sum(1 for sh in sheets if sh.kind == KIND_ONCALL),
                   "overtime": sum(1 for sh in sheets if sh.kind == KIND_OVERTIME),
                   "employees": len({sh.user_id for sh in sheets})},
        "cutoff": cutoff,
    })


@router.post("/{year}/{month}/sent", dependencies=[Depends(verify_csrf)])
def mark_sent(request: Request, year: int, month: int, background: BackgroundTasks, confirm: Optional[str] = Form(None),
              note: str = Form(""), me: User = Depends(payroll_user), db: Session = Depends(get_db)):
    check_month(year, month)
    url = f"/payroll?year={year}&month={month}"
    s = get_settings()
    if not confirm:
        flash(request, "Tick the confirmation: you sent both reports to Finance.", "error")
        return redirect(url)
    sheets = workflow.period_sheets(db, year, month)
    for kind, code in (("oncall", KIND_ONCALL), ("overtime", KIND_OVERTIME)):
        last = workflow.last_export(db, me, kind, year, month)
        current = workflow.period_digest([sh for sh in sheets if sh.kind == code])
        if last is None or last[1] != current:
            flash(request, f"Download the {'on-call' if kind == 'oncall' else 'overtime'} report again first: it was not "
                           "downloaded yet, or sheets were approved since your last download (what you send must be what is recorded).", "error")
            return redirect(url)
    try:
        run = workflow.mark_sent(db, s, me, year, month, note)
        db.commit()
    except workflow.WorkflowError as exc:
        db.rollback()
        flash(request, str(exc), "error")
        return redirect(url)
    flash(request, f"Recorded: the {month_label(year, month)} on-call and overtime reports were sent to Finance for payment. "
                   "Anything approved from now on is paid with the following month.", "success")
    notify_payroll_sent(background, run)
    return redirect(url)


XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


def _download(kind: str, year: int, month: int, me: User, db: Session) -> Response:
    """Whole-organisation report of a payroll period. The download is audited with a fingerprint of its content."""
    check_month(year, month)
    if kind == "control":
        data, name = build_export(db, me, year, month), f"IT_OnCall_Overtime_{year}{month:02d}.xlsx"
        workflow.audit(db, me, "export_control", "period", None, period=f"{year}-{month:02d}", scope="all")
    else:
        code = KIND_ONCALL if kind == "oncall" else KIND_OVERTIME
        build = build_oncall_export if kind == "oncall" else build_overtime_export
        data = build(db, me, year, month)
        sheets = workflow.period_sheets(db, year, month, code)
        workflow.audit(db, me, f"export_{kind}", "period", None, period=f"{year}-{month:02d}", scope="all",
                       digest=workflow.period_digest(sheets), sheets=len(sheets))
        name = f"Y{year % 100:02d}M{month:02d}_IT_{'OnCall' if kind == 'oncall' else 'Overtime'}.xlsx"
    db.commit()
    return Response(content=data, media_type=XLSX, headers={"Content-Disposition": f'attachment; filename="{name}"'})


@router.get("/export/oncall")
def export_oncall(year: int, month: int, me: User = Depends(payroll_user), db: Session = Depends(get_db)):
    """On-call report in the layout of the on-call sheet used by Finance."""
    return _download("oncall", year, month, me, db)


@router.get("/export/overtime")
def export_overtime(year: int, month: int, me: User = Depends(payroll_user), db: Session = Depends(get_db)):
    """Overtime report in the layout of the overtime sheet used by Finance."""
    return _download("overtime", year, month, me, db)


@router.get("/export/control")
def export_control(year: int, month: int, me: User = Depends(payroll_user), db: Session = Depends(get_db)):
    """Detailed control workbook (summary, payroll codes, line-by-line detail)."""
    return _download("control", year, month, me, db)
