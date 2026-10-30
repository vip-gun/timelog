"""Pure computation functions (no DB access). Rules documented in CLAUDE.md §1-2."""
import datetime as dt
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Dict, Iterable, List, Tuple

from .models import DAY_TYPES, HOLIDAY, PAID, SUNDAY, TIL

ZERO = Decimal("0")


def oncall_day_amount(rate: Decimal, is_expert: bool, intervention: bool) -> Decimal:
    """Experts with an intervention get the daily rate doubled (flat, whatever the duration)."""
    factor = 2 if (is_expert and intervention) else 1
    return (Decimal(rate) * factor).quantize(Decimal("0.01"))


def minutes_to_hours(minutes: int) -> Decimal:
    return (Decimal(minutes) / Decimal(60)).quantize(Decimal("0.01"))


def _overlap(a0: int, a1: int, b0: int, b1: int) -> int:
    return max(0, min(a1, b1) - max(a0, b0))


def night_minutes(start: int, end: int, night_start: int, night_end: int) -> int:
    """Minutes of [start, end) within the night window. Window may wrap midnight (22:00-06:00)."""
    if night_start == night_end:
        return 0
    if night_start > night_end:  # wraps midnight
        return _overlap(start, end, 0, night_end) + _overlap(start, end, night_start, 1440)
    return _overlap(start, end, night_start, night_end)


def split_interval(date: dt.date, start: int, end: int) -> List[Tuple[dt.date, int, int]]:
    """Split a time range into per-calendar-day segments. end <= start means it crosses midnight."""
    if end > start:
        return [(date, start, end)]
    segments = [(date, start, 1440)]
    if end > 0:
        segments.append((date + dt.timedelta(days=1), 0, end))
    return segments


def validate_step(minute: int, step: int) -> None:
    if minute % step:
        marks = ", ".join(f":{m:02d}" for m in range(0, 60, step)) if step < 60 else ":00"
        raise ValueError(f"Overtime is recorded in {step}-minute steps: times must end in {marks}.")


def payroll_period(work_year: int, work_month: int, approved: dt.date, cutoff_day: int,
                   closed: Iterable[Tuple[int, int]] = ()) -> Tuple[int, int]:
    """Payroll month of a sheet approved on `approved`.

    Approvals up to `cutoff_day` of month X+1 are paid with month X; later approvals with the approval month. Never
    earlier than the month the work was done. A period already sent to Finance (`closed`) accepts nothing more: the
    sheet moves on to the next period. Late claims (e.g. August overtime approved on 20 October) therefore land in the
    next payroll export that is still open.
    """
    y, m = approved.year, approved.month
    if approved.day <= cutoff_day:
        y, m = (y, m - 1) if m > 1 else (y - 1, 12)
    period = max((y, m), (work_year, work_month))
    closed = set(closed)
    while period in closed:
        period = (period[0], period[1] + 1) if period[1] < 12 else (period[0] + 1, 1)
    return period


@dataclass
class OvertimeTotals:
    """Aggregated overtime figures of one employee for one month."""

    hours_by_type: Dict[str, Decimal] = field(default_factory=lambda: {t: ZERO for t in DAY_TYPES})
    night_by_type: Dict[str, Decimal] = field(default_factory=lambda: {t: ZERO for t in DAY_TYPES})
    paid_hours: Decimal = ZERO
    til_hours: Decimal = ZERO  # hours worked and compensated as TIL
    til_credit: Decimal = ZERO  # time-in-lieu credited (hours x factor)
    hl100: Decimal = ZERO
    hl101: Decimal = ZERO
    hl111: Decimal = ZERO
    hl130: Decimal = ZERO
    hl121: Decimal = ZERO

    @property
    def total_hours(self) -> Decimal:
        return sum(self.hours_by_type.values(), ZERO)

    @property
    def total_night(self) -> Decimal:
        return sum(self.night_by_type.values(), ZERO)


def til_credit(hours: Decimal, night_hours: Decimal, factors: Tuple[Decimal, Decimal]) -> Decimal:
    """Time-in-lieu credited for one entry: day hours and night hours have different factors."""
    f_day, f_night = factors
    credit = (Decimal(hours) - Decimal(night_hours)) * Decimal(f_day) + Decimal(night_hours) * Decimal(f_night)
    return credit.quantize(Decimal("0.01"))


def overtime_totals(entries: Iterable) -> OvertimeTotals:
    """entries: objects with day_type, hours, night_hours, compensation, til_credit (snapshotted)."""
    t = OvertimeTotals()
    for e in entries:
        hours, night = Decimal(e.hours), Decimal(e.night_hours)
        t.hours_by_type[e.day_type] += hours
        t.night_by_type[e.day_type] += night
        if e.compensation == TIL:
            t.til_hours += hours
            t.til_credit += Decimal(e.til_credit)
        else:
            assert e.compensation == PAID
            t.paid_hours += hours
            t.hl100 += hours
            t.hl101 += hours
            t.hl111 += night
            if e.day_type == SUNDAY:
                t.hl130 += hours
            elif e.day_type == HOLIDAY:
                t.hl121 += hours
    return t
