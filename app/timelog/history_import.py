"""Backfill of past on-call and overtime from the Finance workbooks (the layouts written by `export_finance`).

An administrator uploads the on-call and/or overtime workbook of a past period (one tab per work month). Each tab/employee
becomes an **approved sheet already sent to Finance** (history only): the payroll period is the work month, one
`PayrollRun` per period holds the items, nothing is notified or e-mailed and nothing is re-exported as new work.

What the workbooks do not contain is reconstructed, and flagged in the entry text:
- on-call: day type from the holiday table, rate from the workbook's pricing row (else the user's grade), amount = rate
  doubled for an Expert intervention (the "- Incidents/calls" row);
- overtime: the workbook only has hours per day, so each day becomes one entry starting at 00:00 whose length is the hours
  (times are unknown); night hours per day type and the TIL credit are spread over that type's days so the totals match
  the workbook exactly.

Existing sheets are never touched: a user/month that already has a sheet of that kind is skipped.
"""
import datetime as dt
import io
from collections import defaultdict
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Dict, List, Optional, Tuple

from openpyxl import load_workbook
from sqlalchemy import distinct, func, select
from sqlalchemy.orm import Session

from . import calc, teams as team_utils
from .calendar_utils import day_type, holiday_set
from .config import Settings
from .models import (
    APPROVED, HOLIDAY, KIND_ONCALL, KIND_OVERTIME, PAID, SATURDAY, SUNDAY, TIL, WEEKDAY, OnCallDay,
    OvertimeEntry, PayrollRun, PayrollRunItem, Sheet, User, utcnow,
)
from .workflow import audit, grade_of

MAX_BYTES = 15 * 1024 * 1024
FIRST_DAY_COL, DAY_COLS = 4, 31
IMPORT_NOTE = "Imported from a Finance workbook (history)"
NIGHT_COLS = {WEEKDAY: 40, SATURDAY: 41, SUNDAY: 42, HOLIDAY: 43}   # AN..AQ
CREDIT_COLS = (44, 50)                                             # AR / AX


class ImportError_(Exception):
    pass


@dataclass
class OnCallRecord:
    name: str
    year: int
    month: int
    tab: str
    days: Dict[dt.date, Tuple[Optional[Decimal], bool]] = field(default_factory=dict)   # date -> (rate, intervention)


@dataclass
class OvertimeRecord:
    name: str
    year: int
    month: int
    tab: str
    comp: str
    days: Dict[dt.date, Decimal] = field(default_factory=dict)
    night: Dict[str, Decimal] = field(default_factory=dict)
    credit: Decimal = Decimal(0)


@dataclass
class Outcome:
    created: List[dict] = field(default_factory=list)
    skipped: List[dict] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    runs_created: int = 0
    runs_extended: int = 0


# ------------------------------------------------------------------ reading
def _num(v) -> Optional[Decimal]:
    if isinstance(v, bool) or v is None or v == "":
        return None
    try:
        return Decimal(str(v))
    except Exception:
        return None


def _date(v) -> Optional[dt.date]:
    if isinstance(v, dt.datetime):
        return v.date()
    return v if isinstance(v, dt.date) else None


def _open(data: bytes):
    if len(data) > MAX_BYTES:
        raise ImportError_(f"File too large (max {MAX_BYTES // (1024 * 1024)} MB).")
    try:
        return load_workbook(io.BytesIO(data), data_only=True)
    except Exception as exc:
        raise ImportError_("The file is not a readable .xlsx workbook.") from exc


def parse_oncall(data: bytes) -> Tuple[List[OnCallRecord], List[str]]:
    wb, records, warnings = _open(data), [], []
    for ws in wb.worksheets:
        first = _date(ws.cell(3, FIRST_DAY_COL).value)
        if first is None:
            continue                                         # Support tab etc.
        if "late" in ws.title.lower():
            warnings.append(f"Tab '{ws.title}' skipped: late-claim tabs cannot be placed in a payroll period.")
            continue
        dates = {c: _date(ws.cell(3, c).value) for c in range(FIRST_DAY_COL, FIRST_DAY_COL + DAY_COLS)}
        title = next((r for r in range(7, ws.max_row + 1) for c in range(1, 7)
                      if isinstance(ws.cell(r, c).value, str) and "PRICING SHEET" in ws.cell(r, c).value.upper()), None)
        last_emp = (title - 1) if title else ws.max_row
        by_name: Dict[str, OnCallRecord] = {}
        for r in range(7, last_emp + 1):
            label = ws.cell(r, 2).value
            if not isinstance(label, str) or not label.strip():
                continue
            label = label.strip()
            is_inc = label.lower().endswith("incidents/calls")
            name = label.rsplit(" - ", 1)[0].strip() if is_inc else label
            marks = [c for c, d in dates.items() if d and (d.year, d.month) == (first.year, first.month)
                     and (_num(ws.cell(r, c).value) or 0) > 0]
            has_total = ws.cell(r, 35).value is not None
            if not is_inc and not marks and not has_total:
                continue                                      # team header row
            rec = by_name.get(name)
            if rec is None and not is_inc:
                rec = by_name[name] = OnCallRecord(name, first.year, first.month, ws.title)
            for c in marks:
                if is_inc:
                    if rec is not None and dates[c] in rec.days:
                        rec.days[dates[c]] = (rec.days[dates[c]][0], True)
                else:
                    rate = _num(ws.cell(r + title, c).value) if title else None
                    rec.days[dates[c]] = (rate, False)
        records += [r for r in by_name.values() if r.days]
    if not records:
        raise ImportError_("No on-call data found: expected tabs with the dates in row 3 (columns D..AH) and one row per employee.")
    return records, warnings


def parse_overtime(data: bytes) -> Tuple[List[OvertimeRecord], List[str]]:
    wb, records, warnings = _open(data), [], []
    for ws in wb.worksheets:
        first = _date(ws.cell(2, 1).value)
        if first is None or not isinstance(ws.cell(2, FIRST_DAY_COL).value, (dt.date, dt.datetime)):
            continue                                         # READ FIRST etc.
        if "late" in ws.title.lower():
            warnings.append(f"Tab '{ws.title}' skipped: late-claim tabs cannot be placed in a payroll period.")
            continue
        dates = {c: _date(ws.cell(2, c).value) for c in range(FIRST_DAY_COL, FIRST_DAY_COL + DAY_COLS)}
        for r in range(4, ws.max_row + 1):
            last, firstn, comp = ws.cell(r, 1).value, ws.cell(r, 2).value, ws.cell(r, 3).value
            if not isinstance(comp, str) or comp.strip().lower() not in ("paid", "til") or not (last or firstn):
                continue
            rec = OvertimeRecord(f"{firstn or ''} {last or ''}".strip(), first.year, first.month, ws.title,
                                 TIL if comp.strip().lower() == "til" else PAID)
            for c, d in dates.items():
                h = _num(ws.cell(r, c).value)
                if d and (d.year, d.month) == (first.year, first.month) and h and h > 0:
                    rec.days[d] = h
            rec.night = {t: _num(ws.cell(r, c).value) or Decimal(0) for t, c in NIGHT_COLS.items()}
            rec.credit = next((v for v in (_num(ws.cell(r, c).value) for c in CREDIT_COLS) if v), Decimal(0))
            if rec.days:
                records.append(rec)
    if not records:
        raise ImportError_("No overtime data found: expected tabs with the month in A2, dates in row 2 and one row per employee and Paid/TIL.")
    return records, warnings


# ------------------------------------------------------------------ matching and applying
def _norm(s: str) -> str:
    return " ".join((s or "").lower().replace(",", " ").split())


def user_index(db: Session) -> Dict[str, List[User]]:
    idx: Dict[str, List[User]] = defaultdict(list)
    for u in db.scalars(select(User)):
        keys = {_norm(f"{u.first_name} {u.last_name}"), _norm(f"{u.last_name} {u.first_name}"), _norm(u.full_name),
                _norm(u.username)}
        for k in keys:
            if k and u not in idx[k]:
                idx[k].append(u)
    return idx


def _find(idx, name: str):
    hits = idx.get(_norm(name), [])
    if not hits:
        return None, "No user with this name"
    if len(hits) > 1:
        return None, "Several users match this name"
    return hits[0], ""


def _sheet_exists(db: Session, user: User, kind: str, year: int, month: int) -> bool:
    return db.scalar(select(Sheet.id).where(Sheet.user_id == user.id, Sheet.kind == kind, Sheet.year == year,
                                            Sheet.month == month)) is not None


def _new_sheet(db: Session, user: User, kind: str, rec, admin: User, now: dt.datetime) -> Sheet:
    grade = grade_of(db, user)
    sh = Sheet(user_id=user.id, kind=kind, year=rec.year, month=rec.month, seq=1, status=APPROVED,
               payroll_year=rec.year, payroll_month=rec.month, employee_comment=IMPORT_NOTE,
               decision_comment=IMPORT_NOTE, submitted_at=now, decided_at=now, decided_by_id=admin.id,
               snapshot_grade_code=grade.code, snapshot_is_expert=user.is_expert)
    sh.user = user
    db.add(sh)
    db.flush()
    return sh


def apply_import(db: Session, s: Settings, admin: User, oncall: List[OnCallRecord], overtime: List[OvertimeRecord],
                 warnings: List[str], dry_run: bool) -> Outcome:
    out = Outcome(warnings=list(warnings))
    idx = user_index(db)
    now = utcnow()
    holidays: set = set()
    years = {r.year for r in oncall + overtime}
    if years:
        holidays = holiday_set(db, dt.date(min(years), 1, 1), dt.date(max(years) + 1, 1, 31))
    made: List[Sheet] = []

    def skip(kind, rec, reason):
        out.skipped.append({"kind": kind, "name": rec.name, "period": f"{rec.year}-{rec.month:02d}", "reason": reason})

    for rec in oncall:
        user, why = _find(idx, rec.name)
        if user is None:
            skip(KIND_ONCALL, rec, why)
        elif user.is_management:
            skip(KIND_ONCALL, rec, "Management accounts do not declare on-call")
        elif not team_utils.oncall_enabled(db, user):
            skip(KIND_ONCALL, rec, "The user's team does not do on-call")
        elif _sheet_exists(db, user, KIND_ONCALL, rec.year, rec.month):
            skip(KIND_ONCALL, rec, "Already has an on-call sheet for this month")
        else:
            sh = _new_sheet(db, user, KIND_ONCALL, rec, admin, now)
            grade = grade_of(db, user)
            total = Decimal(0)
            for d, (rate, intervention) in sorted(rec.days.items()):
                dtype = day_type(d, holidays)
                r = rate if rate is not None else grade.rate_for(dtype)
                amount = calc.oncall_day_amount(r, user.is_expert, intervention)
                sh.oncall_days.append(OnCallDay(date=d, intervention=intervention, comment=IMPORT_NOTE if intervention else "",
                                                day_type=dtype, rate=r, amount=amount))
                total += amount
            made.append(sh)
            out.created.append({"kind": KIND_ONCALL, "name": user.full_name, "period": f"{rec.year}-{rec.month:02d}",
                                "detail": f"{len(rec.days)} day(s), {total:.2f}"})

    for rec in overtime:
        user, why = _find(idx, rec.name)
        if user is None:
            skip(KIND_OVERTIME, rec, why)
        elif user.is_management or user.is_expert:
            skip(KIND_OVERTIME, rec, "Experts and Management accounts do not file overtime")
        else:
            sh = next((x for x in made if x.user_id == user.id and x.kind == KIND_OVERTIME
                       and (x.year, x.month) == (rec.year, rec.month)), None)
            if sh is None:
                if _sheet_exists(db, user, KIND_OVERTIME, rec.year, rec.month):
                    skip(KIND_OVERTIME, rec, "Already has an overtime sheet for this month")
                    continue
                sh = _new_sheet(db, user, KIND_OVERTIME, rec, admin, now)
                made.append(sh)
            night_left = dict(rec.night)
            total_h = sum(rec.days.values(), Decimal(0))
            for d, hours in sorted(rec.days.items()):
                dtype = day_type(d, holidays)
                minutes = min(1440, int((hours * 60).to_integral_value()))
                h = calc.minutes_to_hours(minutes)
                night = min(night_left.get(dtype, Decimal(0)), h)
                night_left[dtype] = night_left.get(dtype, Decimal(0)) - night
                credit = (rec.credit * h / total_h).quantize(Decimal("0.01")) if rec.comp == TIL and total_h else Decimal(0)
                sh.overtime_entries.append(OvertimeEntry(
                    date=d, start_minute=0, end_minute=max(15, minutes), hours=h, night_hours=night, day_type=dtype,
                    compensation=rec.comp, til_credit=credit, on_call_intervention=False,
                    description=IMPORT_NOTE + " - times unknown"))
            out.created.append({"kind": KIND_OVERTIME, "name": user.full_name, "period": f"{rec.year}-{rec.month:02d}",
                                "detail": f"{total_h:.2f} h {rec.comp}"})

    # payroll runs: one per period, marked as sent (history)
    by_period: Dict[Tuple[int, int], List[Sheet]] = defaultdict(list)
    for sh in made:
        by_period[(sh.payroll_year, sh.payroll_month)].append(sh)
    for (y, m), sheets in sorted(by_period.items()):
        run = db.scalar(select(PayrollRun).where(PayrollRun.year == y, PayrollRun.month == m))
        if run is None:
            run = PayrollRun(year=y, month=m, sent_by_id=admin.id, note=IMPORT_NOTE, sent_at=now)
            db.add(run)
            db.flush()
            out.runs_created += 1
        else:
            out.runs_extended += 1
        for sh in sheets:
            db.add(PayrollRunItem(run_id=run.id, sheet_id=sh.id))
        run.oncall_sheets += sum(1 for x in sheets if x.kind == KIND_ONCALL)
        run.overtime_sheets += sum(1 for x in sheets if x.kind == KIND_OVERTIME)
        db.flush()
        run.employees = db.scalar(select(func.count(distinct(Sheet.user_id))).join(
            PayrollRunItem, PayrollRunItem.sheet_id == Sheet.id).where(PayrollRunItem.run_id == run.id)) or 0
    if dry_run:
        db.rollback()
    else:
        audit(db, admin, "history_import", "sheet", None, sheets=len(made), skipped=len(out.skipped),
              runs_created=out.runs_created, runs_extended=out.runs_extended)
        db.commit()
    return out
