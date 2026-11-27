import datetime as dt
from decimal import Decimal
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import Response
from sqlalchemy.orm import Session

from .. import calc, ooh_report, stats_charts, workflow
from ..access import can_view_team
from ..db import get_db
from ..export import late_claims_pending, payroll_sheets, scope_users, work_month_sheets
from ..models import KIND_ONCALL, KIND_OVERTIME, User
from ..security import current_user
from ..web import render
from .common import check_month, sheet_url

router = APIRouter(prefix="/team")


def _require_manager(db: Session, me: User) -> None:
    if not can_view_team(db, me):
        raise HTTPException(status_code=404, detail="Not found")


@router.get("")
def overview(request: Request, year: Optional[int] = None, month: Optional[int] = None, team: str = "",
             months: int = 12, status: str = "all", group: str = "overview",
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
    months = months if months in (6, 12, 24) else 12
    status = status if status in stats_charts.STATUS_SETS else "all"
    window = stats_charts.month_window(year, month, months)
    group = "officers" if group in ("officers", "non-experts") else ("experts" if group == "experts" else "overview")
    charts = None
    if group == "overview":
        pass
    elif group == "experts":       # on-call days with an intervention (Experts record interventions on the on-call sheet)
        rep = stats_charts.intervention_report(db, [u for u in users if u.is_expert], window, stats_charts.STATUS_SETS[status])
        charts = ooh_report.chart_context(rep, digits=0) if rep.people else None
    else:                        # overtime hours (only non-Experts file overtime)
        rep = stats_charts.overtime_report(db, [u for u in users if not u.is_expert], window, stats_charts.STATUS_SETS[status])
        charts = ooh_report.chart_context(rep) if rep.people else None
    return render(request, "team.html", {
        "chart_months": months, "chart_status": status,
        "chart_group": group, "charts": charts,
        "year": year, "month": month, "team": team, "teams": teams, "rows": rows, "totals": totals,
        "late": late, "pending_late": pending_late, "paid_count": len(paid),
    })
