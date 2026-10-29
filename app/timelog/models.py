import datetime as dt
from decimal import Decimal
from typing import List, Optional

from sqlalchemy import (
    Boolean,
    LargeBinary,
    Date,
    DateTime,
    ForeignKey,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .db import Base

# Day types (see CLAUDE.md §1.1): holiday wins over weekend.
WEEKDAY, SATURDAY, SUNDAY, HOLIDAY = "W", "SAT", "SUN", "H"
DAY_TYPES = (WEEKDAY, SATURDAY, SUNDAY, HOLIDAY)
DAY_TYPE_LABELS = {WEEKDAY: "Weekday", SATURDAY: "Saturday", SUNDAY: "Sunday", HOLIDAY: "Legal holiday"}

KIND_ONCALL, KIND_OVERTIME = "oncall", "overtime"

DRAFT, SUBMITTED, APPROVED, REJECTED = "draft", "submitted", "approved", "rejected"
EDITABLE_STATUSES = (DRAFT, REJECTED)

PAID, TIL = "PAID", "TIL"


def utcnow() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)


class Grade(Base):
    __tablename__ = "grades"

    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(String(32), unique=True)
    name: Mapped[str] = mapped_column(String(128))
    rate_weekday: Mapped[Decimal] = mapped_column(Numeric(10, 2))
    rate_saturday: Mapped[Decimal] = mapped_column(Numeric(10, 2))
    rate_sunday: Mapped[Decimal] = mapped_column(Numeric(10, 2))
    rate_holiday: Mapped[Decimal] = mapped_column(Numeric(10, 2))
    is_default: Mapped[bool] = mapped_column(Boolean, default=False)

    def rate_for(self, day_type: str) -> Decimal:
        return {
            WEEKDAY: self.rate_weekday,
            SATURDAY: self.rate_saturday,
            SUNDAY: self.rate_sunday,
            HOLIDAY: self.rate_holiday,
        }[day_type]


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    username: Mapped[str] = mapped_column(String(128), unique=True, index=True)
    auth_source: Mapped[str] = mapped_column(String(16), default="local")  # local | ldap
    password_hash: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    ldap_dn: Mapped[Optional[str]] = mapped_column(String(512), unique=True, nullable=True)
    first_name: Mapped[str] = mapped_column(String(128), default="")
    last_name: Mapped[str] = mapped_column(String(128), default="")
    display_name: Mapped[str] = mapped_column(String(256), default="")
    email: Mapped[str] = mapped_column(String(256), default="")
    employee_id: Mapped[str] = mapped_column(String(64), default="")
    team: Mapped[str] = mapped_column(String(128), default="")

    manager_id: Mapped[Optional[int]] = mapped_column(ForeignKey("users.id"), nullable=True, index=True)
    # When True, LDAP sync does not overwrite manager_id (admin override).
    manager_locked: Mapped[bool] = mapped_column(Boolean, default=False)
    grade_id: Mapped[Optional[int]] = mapped_column(ForeignKey("grades.id"), nullable=True)

    is_expert: Mapped[bool] = mapped_column(Boolean, default=False)
    is_admin: Mapped[bool] = mapped_column(Boolean, default=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)

    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow)
    last_login_at: Mapped[Optional[dt.datetime]] = mapped_column(DateTime, nullable=True)

    manager: Mapped[Optional["User"]] = relationship(remote_side=[id], back_populates="reports")
    reports: Mapped[List["User"]] = relationship(back_populates="manager")
    grade: Mapped[Optional[Grade]] = relationship()

    @property
    def full_name(self) -> str:
        if self.display_name:
            return self.display_name
        name = f"{self.first_name} {self.last_name}".strip()
        return name or self.username

    @property
    def sort_key(self):
        return (self.team.lower(), self.last_name.lower(), self.first_name.lower(), self.username)


class Holiday(Base):
    __tablename__ = "holidays"

    id: Mapped[int] = mapped_column(primary_key=True)
    date: Mapped[dt.date] = mapped_column(Date, unique=True, index=True)
    name: Mapped[str] = mapped_column(String(128))


class OvertimeRule(Base):
    """TIL credit multipliers per day type (CLAUDE.md §1.2)."""

    __tablename__ = "overtime_rules"

    day_type: Mapped[str] = mapped_column(String(8), primary_key=True)
    til_factor: Mapped[Decimal] = mapped_column(Numeric(5, 2))
    til_factor_night: Mapped[Decimal] = mapped_column(Numeric(5, 2))


class Sheet(Base):
    """One monthly declaration (on-call or overtime) of one user; unit of approval."""

    __tablename__ = "sheets"
    __table_args__ = (UniqueConstraint("user_id", "kind", "year", "month", "seq"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    kind: Mapped[str] = mapped_column(String(16))
    year: Mapped[int] = mapped_column(Integer)
    month: Mapped[int] = mapped_column(Integer)
    # 1 = regular monthly sheet; 2+ = supplementary overtime sheet filed after the month was approved
    seq: Mapped[int] = mapped_column(Integer, default=1)
    status: Mapped[str] = mapped_column(String(16), default=DRAFT)
    # Month in which Finance pays this sheet; set at approval (CLAUDE.md §3, late claims)
    payroll_year: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    payroll_month: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)

    employee_comment: Mapped[str] = mapped_column(Text, default="")
    decision_comment: Mapped[str] = mapped_column(Text, default="")
    submitted_at: Mapped[Optional[dt.datetime]] = mapped_column(DateTime, nullable=True)
    decided_at: Mapped[Optional[dt.datetime]] = mapped_column(DateTime, nullable=True)
    decided_by_id: Mapped[Optional[int]] = mapped_column(ForeignKey("users.id"), nullable=True)

    # Snapshots taken at submission (CLAUDE.md §2)
    snapshot_grade_code: Mapped[str] = mapped_column(String(32), default="")
    snapshot_is_expert: Mapped[bool] = mapped_column(Boolean, default=False)

    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)

    user: Mapped[User] = relationship(foreign_keys=[user_id])
    decided_by: Mapped[Optional[User]] = relationship(foreign_keys=[decided_by_id])
    oncall_days: Mapped[List["OnCallDay"]] = relationship(
        back_populates="sheet", cascade="all, delete-orphan", order_by="OnCallDay.date"
    )
    overtime_entries: Mapped[List["OvertimeEntry"]] = relationship(
        back_populates="sheet",
        cascade="all, delete-orphan",
        order_by="(OvertimeEntry.date, OvertimeEntry.start_minute)",
    )

    signatures: Mapped[List["Signature"]] = relationship(
        back_populates="sheet", cascade="all, delete-orphan", order_by="Signature.id"
    )

    @property
    def editable(self) -> bool:
        return self.status in EDITABLE_STATUSES

    @property
    def signature(self) -> Optional["Signature"]:
        """Signature of the current submission (None once back to draft/rejected and not re-signed)."""
        if self.status not in (SUBMITTED, APPROVED) or not self.signatures:
            return None
        latest = self.signatures[-1]
        return latest if self.submitted_at is None or latest.signed_at >= self.submitted_at else None

    @property
    def is_late(self) -> bool:
        """Paid in a later payroll period than the month the work was done."""
        return self.payroll_year is not None and (self.payroll_year, self.payroll_month) != (self.year, self.month)


class Signature(Base):
    """Employee signature captured when a sheet is submitted (CLAUDE.md §3, signatures)."""

    __tablename__ = "signatures"

    id: Mapped[int] = mapped_column(primary_key=True)
    sheet_id: Mapped[int] = mapped_column(ForeignKey("sheets.id", ondelete="CASCADE"), index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    signed_at: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow)
    statement: Mapped[str] = mapped_column(Text)
    image_png: Mapped[bytes] = mapped_column(LargeBinary)
    content_digest: Mapped[str] = mapped_column(String(64))  # SHA-256 of the signed sheet content
    ip: Mapped[str] = mapped_column(String(64), default="")
    user_agent: Mapped[str] = mapped_column(String(256), default="")

    sheet: Mapped["Sheet"] = relationship(back_populates="signatures")
    user: Mapped["User"] = relationship()


class SavedSignature(Base):
    """A user's stored signature picture (uploaded or drawn), offered again on later submissions."""

    __tablename__ = "saved_signatures"

    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), primary_key=True)
    image_png: Mapped[bytes] = mapped_column(LargeBinary)
    updated_at: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow)


class OnCallDay(Base):
    __tablename__ = "oncall_days"
    __table_args__ = (UniqueConstraint("sheet_id", "date"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    sheet_id: Mapped[int] = mapped_column(ForeignKey("sheets.id", ondelete="CASCADE"), index=True)
    date: Mapped[dt.date] = mapped_column(Date)
    intervention: Mapped[bool] = mapped_column(Boolean, default=False)
    comment: Mapped[str] = mapped_column(String(500), default="")
    incident_ref: Mapped[str] = mapped_column(String(128), default="")
    day_type: Mapped[str] = mapped_column(String(8))
    rate: Mapped[Decimal] = mapped_column(Numeric(10, 2))
    amount: Mapped[Decimal] = mapped_column(Numeric(10, 2))

    sheet: Mapped[Sheet] = relationship(back_populates="oncall_days")


class OvertimeEntry(Base):
    __tablename__ = "overtime_entries"

    id: Mapped[int] = mapped_column(primary_key=True)
    sheet_id: Mapped[int] = mapped_column(ForeignKey("sheets.id", ondelete="CASCADE"), index=True)
    date: Mapped[dt.date] = mapped_column(Date)
    start_minute: Mapped[int] = mapped_column(Integer)  # 0..1425
    end_minute: Mapped[int] = mapped_column(Integer)  # 15..1440
    hours: Mapped[Decimal] = mapped_column(Numeric(5, 2))
    night_hours: Mapped[Decimal] = mapped_column(Numeric(5, 2))
    day_type: Mapped[str] = mapped_column(String(8))
    compensation: Mapped[str] = mapped_column(String(8), default=PAID)
    til_credit: Mapped[Decimal] = mapped_column(Numeric(6, 2), default=0)  # 0 when PAID
    on_call_intervention: Mapped[bool] = mapped_column(Boolean, default=False)
    preapproved: Mapped[bool] = mapped_column(Boolean, default=False)
    incident_ref: Mapped[str] = mapped_column(String(128), default="")
    description: Mapped[str] = mapped_column(String(1000), default="")

    sheet: Mapped[Sheet] = relationship(back_populates="overtime_entries")


class AppSetting(Base):
    """Runtime settings editable by administrators (key/value)."""

    __tablename__ = "app_settings"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[str] = mapped_column(Text, default="")


# Known AppSetting keys and their defaults
SETTING_SHOW_AMOUNTS = "show_amounts_to_employees"
SETTING_TIL_ENABLED = "til_enabled"  # time in lieu as an overtime compensation option
SETTING_SIGNATURE_REQUIRED = "signature_required"  # employees sign when submitting a sheet
SETTING_ONCALL_WEEK_START = "oncall_week_start"  # weekday 0-6 the on-call rotation starts on (env default: Friday)
SETTING_ONCALL_HANDOVER = "oncall_handover"  # HH:MM of the handover, shown to employees (days are booked whole)
SETTING_DEFAULTS = {SETTING_ONCALL_HANDOVER: "18:00", SETTING_SHOW_AMOUNTS: "1", SETTING_TIL_ENABLED: "1", SETTING_SIGNATURE_REQUIRED: "0"}


class AuditLog(Base):
    __tablename__ = "audit_log"

    id: Mapped[int] = mapped_column(primary_key=True)
    at: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow, index=True)
    actor_id: Mapped[Optional[int]] = mapped_column(ForeignKey("users.id"), nullable=True)
    action: Mapped[str] = mapped_column(String(64))
    target_type: Mapped[str] = mapped_column(String(32), default="")
    target_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    details: Mapped[str] = mapped_column(Text, default="")

    actor: Mapped[Optional[User]] = relationship()
