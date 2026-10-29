import datetime as dt
from decimal import Decimal
from types import SimpleNamespace as NS

import pytest

from timelog import calc
from timelog.calendar_utils import day_type, parse_hhmm
from timelog.models import HOLIDAY, SATURDAY, SUNDAY, WEEKDAY

ASSUMPTION = dt.date(2026, 8, 15)  # a Saturday and a legal holiday


def test_day_type_holiday_wins_over_weekend():
    hol = {ASSUMPTION}
    assert day_type(ASSUMPTION, hol) == HOLIDAY
    assert day_type(dt.date(2026, 8, 8), hol) == SATURDAY
    assert day_type(dt.date(2026, 8, 9), hol) == SUNDAY
    assert day_type(dt.date(2026, 8, 10), hol) == WEEKDAY


def test_oncall_amount_expert_doubles_only_with_intervention():
    assert calc.oncall_day_amount(Decimal(45), False, True) == Decimal("45.00")
    assert calc.oncall_day_amount(Decimal(45), True, False) == Decimal("45.00")
    assert calc.oncall_day_amount(Decimal(70), True, True) == Decimal("140.00")


def test_night_minutes_wrapping_window():
    ns, ne = 22 * 60, 6 * 60
    assert calc.night_minutes(20 * 60, 23 * 60, ns, ne) == 60
    assert calc.night_minutes(0, 7 * 60, ns, ne) == 360
    assert calc.night_minutes(9 * 60, 17 * 60, ns, ne) == 0
    assert calc.night_minutes(21 * 60, 1440, ns, ne) == 120


def test_split_interval_crossing_midnight():
    d = dt.date(2026, 8, 8)
    assert calc.split_interval(d, 600, 720) == [(d, 600, 720)]
    assert calc.split_interval(d, 23 * 60, 2 * 60) == [(d, 1380, 1440), (d + dt.timedelta(days=1), 0, 120)]
    assert calc.split_interval(d, 22 * 60, 0) == [(d, 1320, 1440)]


def test_parse_hhmm():
    assert parse_hhmm("24:00") == 1440
    assert parse_hhmm("07:45") == 465


def test_til_credit_and_payroll_codes():
    factors = {WEEKDAY: (Decimal("1.5"), Decimal("1.8")), SUNDAY: (Decimal("2.2"), Decimal("2.5")),
               HOLIDAY: (Decimal("3.5"), Decimal("3.8"))}
    # 2h on weekday of which 1h night -> 1*1.5 + 1*1.8
    assert calc.til_credit(Decimal(2), Decimal(1), factors[WEEKDAY]) == Decimal("3.30")
    entries = [
        NS(day_type=WEEKDAY, hours=Decimal(2), night_hours=Decimal(1), compensation="PAID", til_credit=0),
        NS(day_type=SUNDAY, hours=Decimal(3), night_hours=Decimal(0), compensation="PAID", til_credit=0),
        NS(day_type=HOLIDAY, hours=Decimal(1), night_hours=Decimal(0), compensation="PAID", til_credit=0),
        NS(day_type=SUNDAY, hours=Decimal(1), night_hours=Decimal(0), compensation="TIL", til_credit=Decimal("2.2")),
    ]
    t = calc.overtime_totals(entries)
    assert t.hl100 == t.hl101 == Decimal(6)
    assert t.hl111 == Decimal(1)
    assert t.hl130 == Decimal(3)
    assert t.hl121 == Decimal(1)  # holiday hours NOT also in HL130
    assert t.til_hours == Decimal(1) and t.til_credit == Decimal("2.2")
    assert t.total_hours == Decimal(7)


def test_validate_step_30_minutes():
    calc.validate_step(18 * 60 + 30, 30)
    with pytest.raises(ValueError):
        calc.validate_step(18 * 60 + 15, 30)


def test_payroll_period_cutoff():
    # August work approved by the 10th of September -> August payroll
    assert calc.payroll_period(2026, 8, dt.date(2026, 9, 10), 10) == (2026, 8)
    # approved after the cut-off -> next open period
    assert calc.payroll_period(2026, 8, dt.date(2026, 9, 11), 10) == (2026, 9)
    assert calc.payroll_period(2026, 8, dt.date(2026, 10, 3), 10) == (2026, 9)
    assert calc.payroll_period(2026, 8, dt.date(2026, 10, 20), 10) == (2026, 10)
    # never earlier than the work month; year boundary
    assert calc.payroll_period(2026, 10, dt.date(2026, 10, 5), 10) == (2026, 10)
    assert calc.payroll_period(2026, 12, dt.date(2027, 1, 4), 10) == (2026, 12)
