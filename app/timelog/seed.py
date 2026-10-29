"""Idempotent initial data, taken from the source spreadsheets (CLAUDE.md §1)."""
import datetime as dt
import logging
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from .config import Settings
from .models import HOLIDAY, SATURDAY, SUNDAY, WEEKDAY, Grade, Holiday, OvertimeRule, User
from .security import hash_password

log = logging.getLogger(__name__)

# Luxembourg legal holidays: union of the On-call `Support` tab and the Overtime `READ FIRST` tab.
HOLIDAYS = [
    (dt.date(2025, 12, 24), "Christmas Eve (CBA)"),
    (dt.date(2025, 12, 25), "Christmas Day"),
    (dt.date(2025, 12, 26), "St. Stephen's Day"),
    (dt.date(2026, 1, 1), "New Year's Day"),
    (dt.date(2026, 4, 6), "Easter Monday"),
    (dt.date(2026, 5, 1), "Labour Day"),
    (dt.date(2026, 5, 9), "Europe Day"),
    (dt.date(2026, 5, 14), "Ascension Day"),
    (dt.date(2026, 5, 25), "Whit Monday"),
    (dt.date(2026, 6, 23), "National Day"),
    (dt.date(2026, 8, 15), "Assumption Day"),
    (dt.date(2026, 11, 1), "All Saints' Day"),
    (dt.date(2026, 12, 24), "Christmas Eve (CBA)"),
    (dt.date(2026, 12, 25), "Christmas Day"),
    (dt.date(2026, 12, 26), "St. Stephen's Day"),
    (dt.date(2027, 1, 1), "New Year's Day"),
]

TIL_RULES = {
    WEEKDAY: (Decimal("1.5"), Decimal("1.8")),
    SATURDAY: (Decimal("1.5"), Decimal("1.8")),
    SUNDAY: (Decimal("2.2"), Decimal("2.5")),
    HOLIDAY: (Decimal("3.5"), Decimal("3.8")),
}


def seed(db: Session, s: Settings) -> None:
    if db.scalar(select(Grade.id).limit(1)) is None:
        db.add(Grade(code="STD", name="Standard", rate_weekday=Decimal(45), rate_saturday=Decimal(70),
                     rate_sunday=Decimal(70), rate_holiday=Decimal(90), is_default=True))
        log.info("Seeded default grade STD")
    if db.scalar(select(Holiday.id).limit(1)) is None:
        for date, name in HOLIDAYS:
            db.add(Holiday(date=date, name=name))
        log.info("Seeded %d holidays", len(HOLIDAYS))
    existing_rules = set(db.scalars(select(OvertimeRule.day_type)))
    for day_type, (f_day, f_night) in TIL_RULES.items():
        if day_type not in existing_rules:
            db.add(OvertimeRule(day_type=day_type, til_factor=f_day, til_factor_night=f_night))

    if s.bootstrap_admin_username and s.bootstrap_admin_password:
        has_admin = db.scalar(select(User.id).where(User.is_admin.is_(True)).limit(1)) is not None
        if not has_admin:
            username = s.bootstrap_admin_username.lower()
            user = db.scalar(select(User).where(User.username == username))
            if user is None:
                user = User(username=username, auth_source="local", display_name="Administrator")
                db.add(user)
            user.password_hash = hash_password(s.bootstrap_admin_password)
            user.is_admin = True
            user.is_active = True
            log.warning("Bootstrap admin '%s' created - change its password or disable it after LDAP setup.", username)
    db.commit()
