import calendar
import datetime as dt
from typing import Iterable, List, Set, Tuple

from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import HOLIDAY, SATURDAY, SUNDAY, WEEKDAY, Holiday

MONTH_NAMES = [
    "", "January", "February", "March", "April", "May", "June",
    "July", "August", "September", "October", "November", "December",
]


def day_type(date: dt.date, holidays: Set[dt.date]) -> str:
    """Holiday takes precedence over weekend (CLAUDE.md §1.1, §1.3)."""
    if date in holidays:
        return HOLIDAY
    wd = date.weekday()
    if wd == 5:
        return SATURDAY
    if wd == 6:
        return SUNDAY
    return WEEKDAY


def month_days(year: int, month: int) -> List[dt.date]:
    n = calendar.monthrange(year, month)[1]
    return [dt.date(year, month, d) for d in range(1, n + 1)]


def rotation_week_start(date: dt.date, start_weekday: int) -> dt.date:
    """First day of the on-call rotation week containing `date` (default rotation: Friday -> Thursday)."""
    return date - dt.timedelta(days=(date.weekday() - start_weekday) % 7)


def rotation_week(start: dt.date) -> List[dt.date]:
    return [start + dt.timedelta(days=i) for i in range(7)]


def month_bounds(year: int, month: int) -> Tuple[dt.date, dt.date]:
    days = month_days(year, month)
    return days[0], days[-1]


def holiday_set(db: Session, start: dt.date, end: dt.date) -> Set[dt.date]:
    rows: Iterable[dt.date] = db.scalars(
        select(Holiday.date).where(Holiday.date >= start, Holiday.date <= end)
    )
    return set(rows)


def holiday_names(db: Session, start: dt.date, end: dt.date) -> dict:
    rows = db.scalars(select(Holiday).where(Holiday.date >= start, Holiday.date <= end))
    return {h.date: h.name for h in rows}


def valid_month(year: int, month: int) -> bool:
    return 2000 <= year <= 2100 and 1 <= month <= 12


def shift_month(year: int, month: int, delta: int) -> Tuple[int, int]:
    idx = year * 12 + (month - 1) + delta
    return idx // 12, idx % 12 + 1


def month_label(year: int, month: int) -> str:
    return f"{MONTH_NAMES[month]} {year}"


def fmt_minutes(m: int) -> str:
    return f"{m // 60:02d}:{m % 60:02d}"


def parse_hhmm(value: str) -> int:
    """Parse 'HH:MM' into minutes since midnight. '24:00' is accepted (end of day)."""
    value = (value or "").strip()
    try:
        h_str, m_str = value.split(":")[:2]
        h, m = int(h_str), int(m_str)
    except ValueError:
        raise ValueError(f"Invalid time '{value}' (expected HH:MM).")
    if not (0 <= m < 60) or not (0 <= h <= 24) or (h == 24 and m != 0):
        raise ValueError(f"Invalid time '{value}'.")
    return h * 60 + m
