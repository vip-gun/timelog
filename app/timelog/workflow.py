"""Sheet editing, state transitions, snapshots and audit (CLAUDE.md §2-3)."""
import datetime as dt
import json
from dataclasses import dataclass
from decimal import Decimal
from typing import Dict, List, Optional, Set, Tuple

from sqlalchemy import select
from sqlalchemy.orm import Session

from . import calc
from .app_settings import get_flag, payroll_cutoff
from .calendar_utils import day_type, holiday_set, month_bounds, month_label, shift_month
from .config import Settings
from .models import (
    APPROVED, DRAFT, EDITABLE_STATUSES, KIND_ONCALL, KIND_OVERTIME, PAID, REJECTED, SUBMITTED,
    SETTING_SIGNATURE_REQUIRED, SETTING_TIL_ENABLED, TIL, WEEKDAY, AuditLog, Grade, OnCallDay, OvertimeEntry,
    Notification, OvertimeRule, PayrollRun, PayrollRunItem, Sheet, Signature, User, utcnow,
)


class WorkflowError(ValueError):
    """User-facing validation error."""


def audit(db: Session, actor: Optional[User], action: str, target_type: str = "",
          target_id: Optional[int] = None, **details) -> None:
    db.add(AuditLog(
        actor_id=actor.id if actor else None, action=action, target_type=target_type,
        target_id=target_id, details=json.dumps(details, default=str) if details else "",
    ))


# ------------------------------------------------------------------ lookups
def default_grade(db: Session) -> Grade:
    grade = db.scalar(select(Grade).where(Grade.is_default.is_(True)))
    if grade is None:
        grade = db.scalar(select(Grade).order_by(Grade.id))
    if grade is None:
        raise WorkflowError("No grade configured - ask an administrator.")
    return grade


def grade_of(db: Session, user: User) -> Grade:
    return user.grade or default_grade(db)


def til_factors(db: Session) -> Dict[str, Tuple[Decimal, Decimal]]:
    return {r.day_type: (r.til_factor, r.til_factor_night) for r in db.scalars(select(OvertimeRule))}


def get_sheets(db: Session, user: User, kind: str, year: int, month: int) -> List[Sheet]:
    """All sheets of a month, regular sheet first then supplementary ones."""
    return list(db.scalars(select(Sheet).where(
        Sheet.user_id == user.id, Sheet.kind == kind, Sheet.year == year, Sheet.month == month,
    ).order_by(Sheet.seq)))


def get_sheet(db: Session, user: User, kind: str, year: int, month: int) -> Optional[Sheet]:
    """Latest sheet of the month."""
    sheets = get_sheets(db, user, kind, year, month)
    return sheets[-1] if sheets else None


def get_or_create_sheet(db: Session, user: User, kind: str, year: int, month: int) -> Sheet:
    """Latest sheet of the month; for overtime a supplementary sheet is opened when it is already approved."""
    sheet = get_sheet(db, user, kind, year, month)
    if sheet is None or (kind == KIND_OVERTIME and sheet.status == APPROVED):
        sheet = Sheet(user_id=user.id, kind=kind, year=year, month=month, status=DRAFT,
                      seq=(sheet.seq + 1) if sheet else 1)
        sheet.user = user
        db.add(sheet)
        db.flush()
    return sheet


def _require_editable(sheet: Sheet) -> None:
    if sheet.status == SUBMITTED:
        raise WorkflowError(
            f"Your {month_label(sheet.year, sheet.month)} sheet is awaiting validation. "
            "Withdraw the submission to change it.")
    if sheet.status not in EDITABLE_STATUSES:
        raise WorkflowError("This sheet has been approved and can no longer be edited.")


def overtime_window(s: Settings, today: Optional[dt.date] = None) -> Tuple[dt.date, dt.date]:
    """Dates for which overtime may be declared: from the 1st of the oldest open month up to today."""
    today = today or dt.date.today()
    y, m = shift_month(today.year, today.month, -s.overtime_lookback_months)
    return dt.date(y, m, 1), today


# ------------------------------------------------------------------ on-call
@dataclass
class OnCallInput:
    date: dt.date
    intervention: bool = False
    comment: str = ""
    incident_ref: str = ""


def save_oncall(db: Session, sheet: Sheet, days: List[OnCallInput]) -> None:
    """Replace the on-call days of a draft sheet (only days marked on-call are passed)."""
    _require_editable(sheet)
    start, end = month_bounds(sheet.year, sheet.month)
    for d in days:
        if not (start <= d.date <= end):
            raise WorkflowError(f"{d.date} is outside {sheet.year}-{sheet.month:02d}.")
    existing = {d.date: d for d in sheet.oncall_days}
    wanted = {d.date: d for d in days}
    for date, row in existing.items():
        if date not in wanted:
            sheet.oncall_days.remove(row)
    for date, inp in wanted.items():
        row = existing.get(date)
        if row is None:
            row = OnCallDay(date=date, day_type="", rate=Decimal(0), amount=Decimal(0))
            sheet.oncall_days.append(row)
        if sheet.user.is_expert:
            row.intervention = inp.intervention
            row.comment = inp.comment.strip()[:500]
            row.incident_ref = inp.incident_ref.strip()[:128]
        else:
            # Non-experts record interventions (incident, description) on their overtime sheet only.
            row.intervention, row.comment, row.incident_ref = False, "", ""
    recompute_oncall(db, sheet)


def add_oncall_days(db: Session, sheet: Sheet, dates: List[dt.date]) -> List[dt.date]:
    """Mark extra days as on-call on a draft sheet, keeping existing days. Returns the dates added."""
    _require_editable(sheet)
    start, end = month_bounds(sheet.year, sheet.month)
    existing = {d.date for d in sheet.oncall_days}
    added = []
    for date in sorted(set(dates)):
        if not (start <= date <= end):
            raise WorkflowError(f"{date} is outside {sheet.year}-{sheet.month:02d}.")
        if date not in existing:
            sheet.oncall_days.append(OnCallDay(date=date, day_type="", rate=Decimal(0), amount=Decimal(0)))
            added.append(date)
    recompute_oncall(db, sheet)
    return added


def recompute_oncall(db: Session, sheet: Sheet) -> None:
    start, end = month_bounds(sheet.year, sheet.month)
    holidays = holiday_set(db, start, end)
    grade = grade_of(db, sheet.user)
    for row in sheet.oncall_days:
        row.day_type = day_type(row.date, holidays)
        row.rate = grade.rate_for(row.day_type)
        row.amount = calc.oncall_day_amount(row.rate, sheet.user.is_expert, row.intervention)
    sheet.snapshot_grade_code = grade.code
    sheet.snapshot_is_expert = sheet.user.is_expert


def oncall_dates(db: Session, user: User, start: dt.date, end: dt.date) -> Set[dt.date]:
    """Days `user` is booked on-call between start and end, whatever the sheet status (all months)."""
    return set(db.scalars(select(OnCallDay.date).join(Sheet).where(
        Sheet.user_id == user.id, Sheet.kind == KIND_ONCALL, OnCallDay.date >= start, OnCallDay.date <= end)))


def oncall_conflicts(db: Session, sheet: Sheet) -> Dict[dt.date, List[Tuple[User, str]]]:
    """Days of an on-call sheet on which another employee is also on call (submitted or approved sheets).

    Two employees cannot be on call at the same time; drafts do not count, they are not yet declarations."""
    dates = [d.date for d in sheet.oncall_days]
    if not dates:
        return {}
    rows = db.execute(
        select(OnCallDay.date, Sheet.status, User)
        .join(Sheet, OnCallDay.sheet_id == Sheet.id).join(User, Sheet.user_id == User.id)
        .where(OnCallDay.date.in_(dates), Sheet.kind == KIND_ONCALL, Sheet.user_id != sheet.user_id,
               Sheet.status.in_((SUBMITTED, APPROVED)))
        .order_by(OnCallDay.date, User.last_name, User.first_name))
    out: Dict[dt.date, List[Tuple[User, str]]] = {}
    for date, status, user in rows:
        out.setdefault(date, []).append((user, status))
    return out


def oncall_total(sheet: Sheet) -> Decimal:
    return sum((d.amount for d in sheet.oncall_days), Decimal(0))


# ------------------------------------------------------------------ overtime
@dataclass
class OvertimeInput:
    date: dt.date
    start_minute: int
    end_minute: int  # <= start means crossing midnight
    compensation: str = PAID
    preapproved: bool = False
    incident_ref: str = ""
    description: str = ""


def add_overtime(db: Session, s: Settings, sheet: Sheet, inp: OvertimeInput) -> List[OvertimeEntry]:
    _require_editable(sheet)
    if sheet.user.is_expert:
        raise WorkflowError("Expert employees cannot file overtime; declare interventions on the on-call sheet.")
    if inp.compensation not in (PAID, TIL):
        raise WorkflowError("Compensation must be PAID or TIL.")
    if inp.compensation == TIL and not get_flag(db, SETTING_TIL_ENABLED):
        raise WorkflowError("Time in lieu is disabled: overtime can only be paid.")
    for m in (inp.start_minute, inp.end_minute):
        try:
            calc.validate_step(m, s.overtime_step_minutes)
        except ValueError as exc:
            raise WorkflowError(str(exc))
    if inp.start_minute == inp.end_minute:
        raise WorkflowError("Start and end times are identical.")
    if not inp.description.strip():
        raise WorkflowError("Please describe the work performed.")
    first, last = overtime_window(s)
    if inp.date > last:
        raise WorkflowError("Overtime cannot be declared in advance.")
    if inp.date < first:
        raise WorkflowError(f"Overtime older than {s.overtime_lookback_months} months can no longer be declared "
                            "- contact your manager.")

    start, end = month_bounds(sheet.year, sheet.month)
    segments = calc.split_interval(inp.date, inp.start_minute, inp.end_minute)
    for seg_date, _, _ in segments:
        if not (start <= seg_date <= end):
            if seg_date > end:
                raise WorkflowError(
                    "This entry continues after midnight into the next month: enter it until 24:00, "
                    "then add the remaining part as a separate entry starting 00:00 on the 1st.")
            raise WorkflowError(f"{seg_date} is outside {sheet.year}-{sheet.month:02d}.")
    holidays = holiday_set(db, start, end)
    factors = til_factors(db)
    on_call = oncall_dates(db, sheet.user, start, end)

    created = []
    for seg_date, s0, s1 in segments:
        for other in sheet.overtime_entries:
            if other.date == seg_date and s0 < other.end_minute and other.start_minute < s1:
                raise WorkflowError(f"Overlaps with an existing entry on {seg_date:%d/%m/%Y}.")
        dtype = day_type(seg_date, holidays)
        is_on_call = seg_date in on_call  # detected from the on-call sheets, never typed by the user
        if dtype != WEEKDAY and not (is_on_call or inp.preapproved):
            raise WorkflowError(
                f"{seg_date:%d/%m/%Y} is a weekend / holiday and not one of your on-call days: weekend overtime "
                "must be formally pre-approved by your manager (tick \"pre-approved\").")
        entry = OvertimeEntry(
            date=seg_date, start_minute=s0, end_minute=s1, day_type=dtype, compensation=inp.compensation,
            on_call_intervention=is_on_call, preapproved=inp.preapproved,
            incident_ref=inp.incident_ref.strip()[:128], description=inp.description.strip()[:1000],
        )
        _compute_entry(s, entry, dtype, factors)
        sheet.overtime_entries.append(entry)
        created.append(entry)
    db.flush()
    return created


def delete_overtime(db: Session, sheet: Sheet, entry_id: int) -> None:
    _require_editable(sheet)
    for e in sheet.overtime_entries:
        if e.id == entry_id:
            sheet.overtime_entries.remove(e)
            return
    raise WorkflowError("Entry not found.")


def _compute_entry(s: Settings, e: OvertimeEntry, dtype: str, factors: Dict[str, Tuple[Decimal, Decimal]]) -> None:
    e.day_type = dtype
    e.hours = calc.minutes_to_hours(e.end_minute - e.start_minute)
    e.night_hours = calc.minutes_to_hours(calc.night_minutes(e.start_minute, e.end_minute, s.night_start, s.night_end))
    e.til_credit = calc.til_credit(e.hours, e.night_hours, factors[dtype]) if e.compensation == TIL else Decimal(0)


def recompute_overtime(db: Session, s: Settings, sheet: Sheet) -> None:
    """Refresh day types / night hours / TIL credit with current holidays and rules (snapshot)."""
    start, end = month_bounds(sheet.year, sheet.month)
    holidays = holiday_set(db, start, end)
    factors = til_factors(db)
    on_call = oncall_dates(db, sheet.user, start, end)
    for e in sheet.overtime_entries:
        _compute_entry(s, e, day_type(e.date, holidays), factors)
        e.on_call_intervention = e.date in on_call  # on-call days may have changed since the entry was added
    sheet.snapshot_is_expert = sheet.user.is_expert


# ------------------------------------------------------------------ transitions
@dataclass
class SignatureInput:
    png: bytes
    statement: str
    ip: str = ""
    user_agent: str = ""


def submit(db: Session, s: Settings, sheet: Sheet, actor: User, comment: str = "",
           signature: Optional[SignatureInput] = None) -> None:
    if actor.id != sheet.user_id:
        raise WorkflowError("Only the owner can submit a sheet.")
    _require_editable(sheet)
    if sheet.kind == KIND_ONCALL:
        if not sheet.oncall_days:
            raise WorkflowError("No on-call day selected - nothing to submit.")
        recompute_oncall(db, sheet)
    else:
        if sheet.user.is_expert:
            raise WorkflowError("Expert employees cannot file overtime.")
        if not sheet.overtime_entries:
            raise WorkflowError("No overtime entry - nothing to submit.")
        recompute_overtime(db, s, sheet)
        unjustified = sorted({e.date for e in sheet.overtime_entries
                              if e.day_type != WEEKDAY and not (e.on_call_intervention or e.preapproved)})
        if unjustified:
            raise WorkflowError(
                "Weekend / holiday overtime on " + ", ".join(f"{d:%d/%m}" for d in unjustified)
                + " is neither on an on-call day nor pre-approved: book the on-call day or delete and re-add "
                  "the entry as pre-approved.")
        if not get_flag(db, SETTING_TIL_ENABLED) and any(e.compensation == TIL for e in sheet.overtime_entries):
            raise WorkflowError("Time in lieu is disabled: delete the TIL entries and add them again as paid.")
    if sheet.user.manager_id is None:
        raise WorkflowError("You have no line manager configured - contact an administrator.")
    if signature is None and get_flag(db, SETTING_SIGNATURE_REQUIRED):
        raise WorkflowError("Please sign your declaration before submitting it.")
    sheet.status = SUBMITTED
    sheet.employee_comment = comment.strip()[:2000]
    sheet.submitted_at = utcnow()
    sheet.decided_at = None
    sheet.decided_by_id = None
    sheet.decision_comment = ""
    digest = None
    if signature is not None:
        from .signatures import sheet_digest

        digest = sheet_digest(sheet)  # after recompute + comment: exactly what the manager will see
        sheet.signatures.append(Signature(
            user_id=actor.id, signed_at=sheet.submitted_at, statement=signature.statement, image_png=signature.png,
            content_digest=digest, ip=signature.ip[:64], user_agent=signature.user_agent[:256]))
    audit(db, actor, "submit", "sheet", sheet.id, kind=sheet.kind, period=f"{sheet.year}-{sheet.month:02d}",
          signed=digest is not None, digest=digest)


def month_submittable(db: Session, user: User, year: int, month: int) -> List[Sheet]:
    """The editable sheets of a month that hold something: what one submission sends together."""
    sheets: List[Sheet] = []
    for kind in (KIND_ONCALL, KIND_OVERTIME):
        if kind == KIND_OVERTIME and user.is_expert:
            continue
        for sheet in get_sheets(db, user, kind, year, month):
            if sheet.editable and (sheet.oncall_days if kind == KIND_ONCALL else sheet.overtime_entries):
                sheets.append(sheet)
    return sheets


def submit_month(db: Session, s: Settings, actor: User, year: int, month: int, comment: str = "",
                 signature: Optional[SignatureInput] = None) -> List[Sheet]:
    """Single submission process: on-call and overtime drafts of the month go to the manager together.

    All or nothing: the caller rolls the session back when a sheet is refused."""
    sheets = month_submittable(db, actor, year, month)
    if not sheets:
        raise WorkflowError(f"Nothing to submit for {month_label(year, month)}: no draft with content.")
    for sheet in sheets:
        submit(db, s, sheet, actor, comment, signature)
    return sheets


def withdraw(db: Session, sheet: Sheet, actor: User) -> None:
    if actor.id != sheet.user_id or sheet.status != SUBMITTED:
        raise WorkflowError("Only a submitted sheet can be withdrawn by its owner.")
    sheet.status = DRAFT
    audit(db, actor, "withdraw", "sheet", sheet.id)


def decide(db: Session, s: Settings, sheet: Sheet, actor: User, approve: bool, comment: str = "") -> None:
    from .access import can_decide

    if not can_decide(actor, sheet):
        raise WorkflowError("You are not allowed to validate this sheet.")
    if sheet.status != SUBMITTED:
        raise WorkflowError("Only submitted sheets can be approved or rejected.")
    if not approve and not comment.strip():
        raise WorkflowError("Please give a reason for the rejection.")
    sheet.status = APPROVED if approve else REJECTED
    sheet.decided_at = utcnow()
    sheet.decided_by_id = actor.id
    sheet.decision_comment = comment.strip()[:2000]
    if approve:
        sheet.payroll_year, sheet.payroll_month = calc.payroll_period(
            sheet.year, sheet.month, sheet.decided_at.date(), payroll_cutoff(db, s), closed_periods(db))
    audit(db, actor, "approve" if approve else "reject", "sheet", sheet.id, comment=sheet.decision_comment,
          payroll_period=f"{sheet.payroll_year}-{sheet.payroll_month:02d}" if approve else None,
          skip_level=(not actor.is_admin and sheet.user.manager_id != actor.id) or None)
    if approve and sheet.is_late:
        notify_user(db, sheet.user, "late",
                    f"Your {_kind_label(sheet)} for {month_label(sheet.year, sheet.month)} was approved on "
                    f"{sheet.decided_at:%d/%m/%Y}, after the Finance cut-off: it will be paid with the "
                    f"{month_label(sheet.payroll_year, sheet.payroll_month)} payroll, not with {month_label(sheet.year, sheet.month)}.")


def reopen(db: Session, sheet: Sheet, actor: User, reason: str) -> None:
    if not actor.is_admin:
        raise WorkflowError("Only administrators can reopen an approved sheet.")
    if sheet.status != APPROVED:
        raise WorkflowError("Only approved sheets can be reopened.")
    if not reason.strip():
        raise WorkflowError("A reason is required.")
    if sheet.payroll_item is not None:
        raise WorkflowError("This sheet was already sent to Finance for payment and can no longer be reopened.")
    if get_sheet(db, sheet.user, sheet.kind, sheet.year, sheet.month).id != sheet.id:
        raise WorkflowError("A supplementary sheet exists for this month - only the latest sheet can be reopened.")
    sheet.status = DRAFT
    sheet.payroll_year = sheet.payroll_month = None
    audit(db, actor, "reopen", "sheet", sheet.id, reason=reason.strip())


# ------------------------------------------------------------------ payroll periods, notices, payment runs
def _kind_label(sheet: Sheet) -> str:
    return "on-call declaration" if sheet.kind == KIND_ONCALL else "overtime declaration"


def notify_user(db: Session, user: User, kind: str, message: str) -> Notification:
    n = Notification(user_id=user.id, kind=kind, message=message)
    db.add(n)
    return n


def closed_periods(db: Session) -> Set[Tuple[int, int]]:
    """Payroll periods already sent to Finance."""
    return {(y, m) for y, m in db.execute(select(PayrollRun.year, PayrollRun.month))}


def expected_period(db: Session, s: Settings, work_year: int, work_month: int,
                    on: Optional[dt.date] = None) -> Tuple[int, int]:
    """Payroll period a sheet of this work month would be paid in if it were approved on `on` (today)."""
    return calc.payroll_period(work_year, work_month, on or dt.date.today(), payroll_cutoff(db, s), closed_periods(db))


def approval_deadline(db: Session, s: Settings, work_year: int, work_month: int) -> Optional[dt.date]:
    """Last day on which an approval is still paid with the work month itself (None: that period is closed)."""
    if (work_year, work_month) in closed_periods(db):
        return None
    ny, nm = shift_month(work_year, work_month, 1)
    return dt.date(ny, nm, payroll_cutoff(db, s))


def timing_message(db: Session, s: Settings, work_year: int, work_month: int) -> Tuple[str, bool]:
    """(text, is_late) telling when a sheet of this work month must be approved to be paid with that month."""
    label = month_label(work_year, work_month)
    expected = expected_period(db, s, work_year, work_month)
    if expected != (work_year, work_month):
        return (f"The Finance cut-off for {label} has passed: even if approved today it will be paid with the "
                f"{month_label(*expected)} payroll.", True)
    deadline = approval_deadline(db, s, work_year, work_month)
    return (f"To be paid with the {label} payroll your line manager must approve it by {deadline:%d/%m/%Y}; "
            "later approvals are paid the following month.", False)


def warn_payroll_timing(db: Session, s: Settings, sheets: List[Sheet]) -> List[str]:
    """After a submission: messages for the user; late ones are also kept as in-app notices."""
    messages = []
    for sheet in sheets:
        text, late = timing_message(db, s, sheet.year, sheet.month)
        text = f"{_kind_label(sheet).capitalize()} {month_label(sheet.year, sheet.month)}: {text}"
        messages.append(text)
        if late:
            notify_user(db, sheet.user, "late", text)
    return messages


def period_sheets(db: Session, year: int, month: int, kind: Optional[str] = None) -> List[Sheet]:
    """Approved sheets of the whole organisation paid in payroll period (year, month)."""
    q = select(Sheet).where(Sheet.status == APPROVED, Sheet.payroll_year == year, Sheet.payroll_month == month)
    if kind:
        q = q.where(Sheet.kind == kind)
    return list(db.scalars(q.order_by(Sheet.id)))


def period_digest(sheets: List[Sheet]) -> str:
    """Fingerprint of the set of sheets in a period: what a report contained."""
    import hashlib

    return hashlib.sha256(",".join(str(i) for i in sorted(s.id for s in sheets)).encode()).hexdigest()[:16]


def mark_sent(db: Session, s: Settings, actor: User, year: int, month: int, note: str = "") -> PayrollRun:
    """IT Procurement sent the reports of period (year, month) to Finance: log it and close the period."""
    if not (actor.is_procurement or actor.is_admin):
        raise WorkflowError("Only IT Procurement can record the payment run.")
    if (year, month) in closed_periods(db):
        raise WorkflowError(f"The {month_label(year, month)} payroll was already sent to Finance.")
    sheets = [sh for sh in period_sheets(db, year, month) if sh.payroll_item is None]
    if not sheets:
        raise WorkflowError(f"There is nothing approved for the {month_label(year, month)} payroll: nothing to send.")
    run = PayrollRun(year=year, month=month, sent_by_id=actor.id, note=note.strip()[:500],
                     oncall_sheets=sum(1 for sh in sheets if sh.kind == KIND_ONCALL),
                     overtime_sheets=sum(1 for sh in sheets if sh.kind == KIND_OVERTIME),
                     employees=len({sh.user_id for sh in sheets}))
    db.add(run)
    db.flush()
    for sh in sheets:
        db.add(PayrollRunItem(run_id=run.id, sheet_id=sh.id))
    audit(db, actor, "payroll_sent", "payroll_run", run.id, period=f"{year}-{month:02d}", oncall=run.oncall_sheets,
          overtime=run.overtime_sheets, employees=run.employees, note=run.note)
    # tell the employees: paid items are on their way, items still waiting for approval move to the next period
    by_user: Dict[int, List[Sheet]] = {}
    for sh in sheets:
        by_user.setdefault(sh.user_id, []).append(sh)
    for sh_list in by_user.values():
        user = sh_list[0].user
        what = " and ".join(sorted({_kind_label(x) + "s" for x in sh_list}))
        notify_user(db, user, "info", f"Your approved {what} for the {month_label(year, month)} payroll were sent to "
                                      f"Finance for payment on {run.sent_at:%d/%m/%Y}.")
    nyear, nmonth = shift_month(year, month, 1)
    waiting = db.scalars(select(Sheet).where(Sheet.status == SUBMITTED, (Sheet.year * 12 + Sheet.month) <= year * 12 + month))
    for sh in waiting:
        notify_user(db, sh.user, "late", f"Your {_kind_label(sh)} for {month_label(sh.year, sh.month)} is still waiting for "
                                         f"approval, but the {month_label(year, month)} payroll was already sent: it will be "
                                         f"paid with the {month_label(nyear, nmonth)} payroll at the earliest.")
    return run


def last_export(db: Session, actor: User, kind: str, year: int, month: int) -> Optional[Tuple[dt.datetime, str]]:
    """(time, digest) of the actor's latest whole-organisation download of the on-call / overtime report of a period."""
    rows = db.scalars(select(AuditLog).where(AuditLog.actor_id == actor.id, AuditLog.action == f"export_{kind}")
                      .order_by(AuditLog.id.desc()).limit(60))
    for row in rows:
        try:
            d = json.loads(row.details or "{}")
        except ValueError:
            continue
        if d.get("period") == f"{year}-{month:02d}" and d.get("scope") == "all":
            return row.at, d.get("digest", "")
    return None


def default_payroll_period(db: Session, s: Settings, today: Optional[dt.date] = None) -> Tuple[int, int]:
    """The period IT Procurement most likely wants: the oldest unsent period holding approved sheets, else the
    period an approval of last month's work would fall into today."""
    today = today or dt.date.today()
    closed = closed_periods(db)
    unsent = sorted({(y, m) for y, m in db.execute(
        select(Sheet.payroll_year, Sheet.payroll_month).where(Sheet.status == APPROVED, Sheet.payroll_year.is_not(None))
        .distinct()) if (y, m) not in closed})
    if unsent:
        return unsent[0]
    py, pm = shift_month(today.year, today.month, -1)
    return calc.payroll_period(py, pm, today, payroll_cutoff(db, s), closed)
