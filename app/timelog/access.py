"""Single place for authorization rules (CLAUDE.md §4)."""
from typing import Dict, List, Set

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import Sheet, User


def subordinate_ids(db: Session, manager: User) -> Set[int]:
    """All users below `manager` in the hierarchy (recursive, cycle-safe)."""
    rows = db.execute(select(User.id, User.manager_id).where(User.manager_id.is_not(None))).all()
    children: Dict[int, List[int]] = {}
    for uid, mid in rows:
        children.setdefault(mid, []).append(uid)
    result: Set[int] = set()
    stack = list(children.get(manager.id, []))
    while stack:
        uid = stack.pop()
        if uid in result or uid == manager.id:
            continue
        result.add(uid)
        stack.extend(children.get(uid, []))
    return result


def is_manager(db: Session, user: User) -> bool:
    return db.scalar(select(User.id).where(User.manager_id == user.id).limit(1)) is not None


def visible_user_ids(db: Session, viewer: User) -> Set[int]:
    """Users whose declarations `viewer` may see (always includes self)."""
    if viewer.is_admin:
        return set(db.scalars(select(User.id)))
    return subordinate_ids(db, viewer) | {viewer.id}


def can_view_user(db: Session, viewer: User, target: User) -> bool:
    if viewer.id == target.id or viewer.is_admin:
        return True
    return target.id in subordinate_ids(db, viewer)


def can_decide(viewer: User, sheet: Sheet) -> bool:
    """Approve/reject: direct line manager or admin, never on one's own sheet."""
    if sheet.user_id == viewer.id:
        return False
    return viewer.is_admin or sheet.user.manager_id == viewer.id


def can_export(db: Session, viewer: User) -> bool:
    return viewer.is_admin or is_manager(db, viewer)


def not_found() -> HTTPException:
    # 404 rather than 403 so the existence of other users' data is not disclosed.
    return HTTPException(status_code=404, detail="Not found")


def require_view_user(db: Session, viewer: User, target_id: int) -> User:
    target = db.get(User, target_id)
    if target is None or not can_view_user(db, viewer, target):
        raise not_found()
    return target


def require_view_sheet(db: Session, viewer: User, sheet_id: int) -> Sheet:
    sheet = db.get(Sheet, sheet_id)
    if sheet is None or not can_view_user(db, viewer, sheet.user):
        raise not_found()
    return sheet
