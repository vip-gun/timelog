import datetime as dt
from decimal import Decimal

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import PlainTextResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .. import calc, workflow
from ..access import subordinate_ids
from ..app_settings import can_see_amounts
from ..calendar_utils import shift_month
from ..db import get_db
from ..models import KIND_ONCALL, KIND_OVERTIME, SUBMITTED, Notification, Sheet, User, utcnow
from ..security import current_user, verify_csrf
from ..version import BUILD
from ..web import redirect, render

router = APIRouter()


@router.get("/healthz", include_in_schema=False)
def healthz(db: Session = Depends(get_db)):
    db.execute(select(1))
    return PlainTextResponse(f"ok {BUILD.version} {BUILD.timestamp}{' ' + BUILD.commit if BUILD.commit else ''}")


@router.get("/")
def dashboard(request: Request, me: User = Depends(current_user), db: Session = Depends(get_db)):
    today = dt.date.today()
    months = [shift_month(today.year, today.month, -d) for d in range(6)]
    mine = {}
    for sheet in db.scalars(select(Sheet).where(Sheet.user_id == me.id).order_by(Sheet.seq)):
        mine[(sheet.kind, sheet.year, sheet.month)] = sheet  # keeps the latest seq
    rows = [
        {"year": y, "month": m, "oncall": mine.get((KIND_ONCALL, y, m)), "overtime": mine.get((KIND_OVERTIME, y, m))}
        for y, m in months
    ]
    pending_q = select(func.count(Sheet.id)).join(User, Sheet.user_id == User.id).where(Sheet.status == SUBMITTED)
    if not me.is_admin:
        pending_q = pending_q.where(User.id.in_(subordinate_ids(db, me)))
    pending = db.scalar(pending_q) or 0

    this_month = list(db.scalars(select(Sheet).where(
        Sheet.user_id == me.id, Sheet.year == today.year, Sheet.month == today.month)))
    oncall = [s for s in this_month if s.kind == KIND_ONCALL]
    entries = [e for s in this_month if s.kind == KIND_OVERTIME for e in s.overtime_entries]
    stats = {
        "oncall_days": sum(len(s.oncall_days) for s in oncall),
        "oncall_amount": sum((workflow.oncall_total(s) for s in oncall), Decimal(0)),
        "ot_hours": calc.overtime_totals(entries).total_hours,
        "interventions": sum(1 for s in oncall for d in s.oncall_days if d.intervention),
    }
    return render(request, "dashboard.html", {"rows": rows, "pending": pending, "stats": stats,
                                              "show_amounts": can_see_amounts(db, me, me)})


@router.post("/notices/{notice_id}/read", dependencies=[Depends(verify_csrf)])
def dismiss_notice(notice_id: int, back: str = Form("/"), me: User = Depends(current_user), db: Session = Depends(get_db)):
    notice = db.get(Notification, notice_id)
    if notice is not None and notice.user_id == me.id and notice.read_at is None:
        notice.read_at = utcnow()
        db.commit()
    return redirect(back if back.startswith("/") and not back.startswith("//") else "/")
