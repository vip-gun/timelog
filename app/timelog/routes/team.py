import datetime as dt
from decimal import Decimal
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import Response
from sqlalchemy.orm import Session

from .. import calc, workflow
from ..access import can_export
from ..db import get_db
from ..export import build_export, late_claims_pending, payroll_sheets, scope_users, work_month_sheets
from ..models import KIND_ONCALL, KIND_OVERTIME, User
from ..security import current_user
from ..web import render
from .common import check_month, sheet_url

router = APIRouter(prefix="/team")


def _require_manager(db: Session, me: User) -> None:
    if not can_export(db, me):
        raise HTTPException(status_code=404, detail="Not found")


@router.get("")
def overview(request: Request, year: Optional[int] = None, month: Optional[int] = None, team: str = "",
             me: User = Depends(current_user), db: Session = Depends(get_db)):
    _require_manager(db, me)
    today = dt.date.today()
    year, month = year or today.year, month or today.month
    check_month(year, month)
    all_users = scope_users(db, me)
    teams = sorted({u.team for u in all_users if u.team})
    users = [u for u in all_users if not team or u.team == team]
    by_id = {u.id: u for u in users}
    current = work_month_sheets(db, users, year, month)
    rows = []
    for u in users:
        oc_list = current.get((u.id, KIND_ONCALL), [])
        ot_list = current.get((u.id, KIND_OVERTIME), [])
        oc = oc_list[-1] if oc_list else None
        ot_entries = [e for s in ot_list for e in s.overtime_entries]
        rows.append({"user": u, "oncall": oc, "overtime": ot_list,
                     "oc_amount": workflow.oncall_total(oc) if oc else Decimal(0),
                     "oc_days": len(oc.oncall_days) if oc else 0,
                     "ot": calc.overtime_totals(ot_entries) if ot_list else None,
                     "direct": u.manager_id == me.id})

    paid = payroll_sheets(db, users, year, month)
    totals = {"oncall": sum((workflow.oncall_total(s) for s in paid if s.kind == KIND_ONCALL), Decimal(0)),
              "hours": sum((calc.overtime_totals(s.overtime_entries).total_hours
                            for s in paid if s.kind == KIND_OVERTIME), Decimal(0))}
    late = [{"sheet": s, "user": by_id[s.user_id], "url": sheet_url(s, me)} for s in paid if s.is_late]
    pending_late = [{"sheet": s, "user": by_id[s.user_id], "url": sheet_url(s, me)}
                    for s in late_claims_pending(db, users, year, month)]
    return render(request, "team.html", {
        "year": year, "month": month, "team": team, "teams": teams, "rows": rows, "totals": totals,
        "late": late, "pending_late": pending_late, "paid_count": len(paid),
    })


@router.get("/export")
def export(year: int, month: int, team: str = "", me: User = Depends(current_user), db: Session = Depends(get_db)):
    _require_manager(db, me)
    check_month(year, month)
    data = build_export(db, me, year, month, team)
    workflow.audit(db, me, "export", "period", None, period=f"{year}-{month:02d}", team=team)
    db.commit()
    suffix = f"_{''.join(ch for ch in team if ch.isalnum())}" if team else ""
    filename = f"IT_OnCall_Overtime_{year}{month:02d}{suffix}.xlsx"
    return Response(
        content=data,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
