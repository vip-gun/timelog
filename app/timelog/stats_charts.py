"""Statistics charts built from what is logged in Timelog (not from a file): overtime hours per person and day type, and
on-call days **with an intervention** (Experts record interventions on the on-call sheet; for everybody else the
intervention is an overtime entry, already counted in the overtime hours). Draft and rejected sheets are left out."""
import datetime as dt
from collections import defaultdict
from typing import Dict, Iterable, List, Tuple

from sqlalchemy import select
from sqlalchemy.orm import Session

from . import ooh_report as ooh
from .models import APPROVED, KIND_ONCALL, KIND_OVERTIME, SUBMITTED, OnCallDay, OvertimeEntry, Sheet, User

STATUS_SETS = {"all": (SUBMITTED, APPROVED), "approved": (APPROVED,)}


def month_window(year: int, month: int, count: int) -> List[str]:
    out = []
    y, m = year, month
    for _ in range(count):
        out.append(f"{y:04d}-{m:02d}")
        y, m = (y - 1, 12) if m == 1 else (y, m - 1)
    return out[::-1]


def _collect(rows: Iterable[Tuple[str, str, str, float, dict]], months: List[str]) -> ooh.Report:
    totals: Dict[str, Dict[str, float]] = defaultdict(dict)
    monthly: Dict[Tuple[str, str], Dict[str, float]] = defaultdict(dict)
    tickets: Dict[str, int] = defaultdict(int)
    detail: List[dict] = []
    for person, month, dtype, amount, item in rows:
        if month not in months:
            continue
        detail.append(item)
        totals[person][dtype] = totals[person].get(dtype, 0.0) + amount
        monthly[(person, month)][dtype] = monthly[(person, month)].get(dtype, 0.0) + amount
        tickets[person] += 1
    detail.sort(key=lambda d: (d["start"], d["person"]))
    return ooh.synthetic_report(totals, monthly, months, tickets, detail)


def _bounds(months: List[str]):
    first, last = months[0], months[-1]
    key = lambda ym: int(ym[:4]) * 12 + int(ym[5:])  # noqa: E731
    return key(first), key(last)


def overtime_report(db: Session, users: List[User], months: List[str], statuses=STATUS_SETS["all"]) -> ooh.Report:
    lo, hi = _bounds(months)
    ids = [u.id for u in users]
    q = (select(OvertimeEntry, Sheet).join(Sheet, OvertimeEntry.sheet_id == Sheet.id)
         .where(Sheet.user_id.in_(ids), Sheet.kind == KIND_OVERTIME, Sheet.status.in_(statuses),
                Sheet.year * 12 + Sheet.month >= lo, Sheet.year * 12 + Sheet.month <= hi))
    by_id = {u.id: u for u in users}
    rows = []
    for e, s in db.execute(q):
        name = by_id[s.user_id].full_name
        day = dt.datetime.combine(e.date, dt.time())
        rows.append((name, f"{s.year:04d}-{s.month:02d}", e.day_type, float(e.hours), {
            "row": e.id, "number": e.incident_ref, "person": name, "description": e.description,
            "start": day + dt.timedelta(minutes=e.start_minute), "end": day + dt.timedelta(minutes=e.end_minute),
            "hours": {e.day_type: float(e.hours)}, "total": float(e.hours), "allday": False}))
    return _collect(rows, months)


def intervention_report(db: Session, users: List[User], months: List[str], statuses=STATUS_SETS["all"]) -> ooh.Report:
    lo, hi = _bounds(months)
    ids = [u.id for u in users]
    q = (select(OnCallDay, Sheet).join(Sheet, OnCallDay.sheet_id == Sheet.id)
         .where(Sheet.user_id.in_(ids), Sheet.kind == KIND_ONCALL, Sheet.status.in_(statuses), OnCallDay.intervention.is_(True),
                Sheet.year * 12 + Sheet.month >= lo, Sheet.year * 12 + Sheet.month <= hi))
    by_id = {u.id: u for u in users}
    rows = []
    for d, s in db.execute(q):
        name = by_id[s.user_id].full_name
        day = dt.datetime.combine(d.date, dt.time())
        rows.append((name, f"{s.year:04d}-{s.month:02d}", d.day_type, 1.0, {
            "row": d.id, "number": d.incident_ref, "person": name, "description": d.comment, "start": day,
            "end": day + dt.timedelta(days=1), "hours": {d.day_type: 1.0}, "total": 1.0, "allday": True}))
    return _collect(rows, months)
