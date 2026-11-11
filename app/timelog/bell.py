"""Header notification bell: unread notices, requests waiting for the viewer, and recent decisions on their sheets."""
import datetime as dt
from dataclasses import dataclass, field
from typing import List, Optional

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .access import subordinate_ids
from .calendar_utils import month_label
from .models import APPROVED, KIND_ONCALL, REJECTED, SUBMITTED, Notification, Sheet, User, utcnow

RECENT_DAYS = 14


@dataclass
class BellItem:
    kind: str            # alert | request | change
    text: str
    url: str = ""
    when: Optional[dt.datetime] = None
    notice_id: Optional[int] = None


@dataclass
class Bell:
    items: List[BellItem] = field(default_factory=list)
    count: int = 0       # things that need attention (not the purely informational "approved" lines)


def _label(sheet: Sheet) -> str:
    kind = "on-call" if sheet.kind == KIND_ONCALL else "overtime"
    return f"{kind} {month_label(sheet.year, sheet.month)}" + (f" (suppl. #{sheet.seq})" if sheet.seq > 1 else "")


def _own_url(sheet: Sheet) -> str:
    return f"/{'oncall' if sheet.kind == KIND_ONCALL else 'overtime'}/{sheet.year}/{sheet.month}"


def build_bell(db: Session, user: User, notices: List[Notification], is_manager: bool) -> Bell:
    bell = Bell()
    total_notices = db.scalar(select(func.count(Notification.id)).where(
        Notification.user_id == user.id, Notification.read_at.is_(None))) or 0
    for n in notices:
        bell.items.append(BellItem("alert" if n.kind == "late" else "change", n.message, "", n.created_at, n.id))
    bell.count += total_notices

    if is_manager or user.is_admin:
        q = select(func.count(Sheet.id)).where(Sheet.status == SUBMITTED, Sheet.user_id != user.id)
        if not user.is_admin:
            q = q.where(Sheet.user_id.in_(subordinate_ids(db, user)))
        waiting = db.scalar(q) or 0
        if waiting:
            bell.items.append(BellItem("request", f"{waiting} sheet{'s' if waiting != 1 else ''} waiting for your approval", "/approvals"))
            bell.count += 1

    rejected = list(db.scalars(select(Sheet).where(Sheet.user_id == user.id, Sheet.status == REJECTED)
                               .order_by(Sheet.decided_at.desc()).limit(5)))
    for s in rejected:
        comment = f": {s.decision_comment}" if s.decision_comment else ""
        bell.items.append(BellItem("alert", f"Your {_label(s)} sheet was rejected{comment}", _own_url(s), s.decided_at))
        bell.count += 1

    since = utcnow() - dt.timedelta(days=RECENT_DAYS)
    approved = db.scalars(select(Sheet).where(Sheet.user_id == user.id, Sheet.status == APPROVED, Sheet.decided_at >= since)
                          .order_by(Sheet.decided_at.desc()).limit(5))
    for s in approved:
        bell.items.append(BellItem("change", f"Your {_label(s)} sheet was approved", _own_url(s), s.decided_at))
    return bell
