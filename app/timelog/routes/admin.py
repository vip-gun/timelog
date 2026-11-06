import datetime as dt
import io
import logging
import re
from pathlib import Path
from decimal import Decimal, InvalidOperation
from typing import List, Optional

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse
from sqlalchemy import delete, extract, func, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .. import backup as backups
from ..access import subordinate_ids
from ..app_settings import (
    LDAP_FIELDS, LDAP_PREFIX, cutoff_schedule, oncall_week, SECRET_FIELDS, clear_ldap_settings, effective_settings, get_flag, ldap_overrides,
    save_ldap_settings, secret_status, set_setting,
)
from ..calendar_utils import parse_hhmm
from ..config import get_settings
from ..ldap_auth import LdapUnavailable, diagnose, lookup_users, sync_user
from ..db import get_db, get_engine
from ..ics_import import MAX_BYTES, IcsError, parse_ics
from .. import teams as team_utils
from ..models import APPROVED, DAY_TYPES, SETTING_ONCALL_HANDOVER, SETTING_ONCALL_WEEK_START, SETTING_PAYROLL_CUTOFF, SETTING_SHOW_AMOUNTS, SETTING_SIGNATURE_REQUIRED, SETTING_TIL_ENABLED, AppSetting, AuditLog, Grade, Holiday, Notification, OnCallDay, OvertimeEntry, OvertimeRule, PayrollRun, SavedSignature, Sheet, Signature, Team, User, utcnow
from ..security import admin_user, check_password_policy, flash, hash_password, verify_csrf
from ..web import redirect, render
from ..workflow import audit

router = APIRouter(prefix="/admin", dependencies=[Depends(admin_user)])


def _dec(value: str, field: str) -> Decimal:
    try:
        d = Decimal(value.replace(",", "."))
    except (InvalidOperation, AttributeError):
        raise ValueError(f"{field}: invalid number")
    if d < 0:
        raise ValueError(f"{field}: must be positive")
    return d


# ------------------------------------------------------------------ users
@router.get("/users")
def users(request: Request, q: str = "", archived: int = 0, team: str = "", db: Session = Depends(get_db)):
    stmt = select(User).where(User.is_archived.is_(True) if archived else User.is_archived.is_not(True))
    if team == NO_TEAM:
        stmt = stmt.where(func.trim(User.team) == "")
    elif team:
        stmt = stmt.where(func.lower(User.team) == team.strip().lower())
    if q:
        like = f"%{q.lower()}%"
        stmt = stmt.where(or_(User.username.ilike(like), User.first_name.ilike(like), User.last_name.ilike(like),
                              User.display_name.ilike(like), User.team.ilike(like)))
    rows = sorted(db.scalars(stmt), key=lambda u: u.sort_key)
    n_archived = db.scalar(select(func.count(User.id)).where(User.is_archived.is_(True)))
    return render(request, "admin/users.html", {
        "users": rows, "q": q, "archived": bool(archived), "n_archived": n_archived, "team": team, "no_team": NO_TEAM,
        "oncall_off": {t.name.strip().lower() for t in db.scalars(select(Team).where(Team.oncall_enabled.is_(False)))},
        **_bulk_ctx(db)})


NO_TEAM = "__none__"


def _manager_candidates(db: Session) -> List[User]:
    return sorted(db.scalars(select(User).where(User.is_active.is_(True), User.is_archived.is_not(True))), key=lambda u: u.full_name.lower())


def _bulk_ctx(db: Session) -> dict:
    return {"managers": _manager_candidates(db), "teams": team_utils.all_team_names(db),
            "grades": list(db.scalars(select(Grade).order_by(Grade.code)))}


def _user_form_ctx(db: Session, user: Optional[User]) -> dict:
    candidates = _manager_candidates(db)
    if user is not None and user.id:
        excluded = subordinate_ids(db, user) | {user.id}
        candidates = [c for c in candidates if c.id not in excluded]
    return {"edit_user": user, "managers": candidates, "teams": team_utils.all_team_names(db),
            "grades": list(db.scalars(select(Grade).order_by(Grade.code)))}


LDAP_USERNAME = re.compile(r"^[a-z0-9][a-z0-9._@$-]{0,127}$")


def _parse_usernames(raw: str) -> List[str]:
    seen: dict = {}
    for token in re.split(r"[\s,;]+", raw.lower()):
        if token:
            seen.setdefault(token, None)
    return list(seen)


@router.get("/users/ldap")
def ldap_users_form(request: Request, db: Session = Depends(get_db)):
    return render(request, "admin/ldap_users.html", {
        "ldap_enabled": effective_settings(db, get_settings()).ldap_enabled, **_bulk_ctx(db)})


@router.post("/users/ldap", dependencies=[Depends(verify_csrf)])
def create_ldap_users(request: Request, usernames: str = Form(""), lookup: Optional[str] = Form(None), team: str = Form(""),
                      manager_id: str = Form(""), is_expert: str = Form(""), grade_id: str = Form(""),
                      me: User = Depends(admin_user), db: Session = Depends(get_db)):
    """Create directory (LDAP) accounts before the people first sign in, so their team, manager and roles can be
    prepared. At first login the account is matched by username and refreshed from the directory as usual, except for
    the team and manager set here, which are locked against the directory."""
    back = "/admin/users/ldap"
    names = _parse_usernames(usernames)
    if not names:
        flash(request, "Enter at least one username.", "error")
        return redirect(back)
    if len(names) > 200:
        flash(request, "At most 200 usernames at a time.", "error")
        return redirect(back)
    bad = [n for n in names if not LDAP_USERNAME.match(n)]
    if bad:
        flash(request, f"Invalid username(s): {', '.join(bad[:5])}.", "error")
        return redirect(back)
    manager = None
    if manager_id:
        manager = db.get(User, int(manager_id)) if manager_id.isdigit() else None
        if manager is None or not manager.is_active or manager.is_archived:
            flash(request, "The chosen line manager is not an active user.", "error")
            return redirect(back)
    grade = None
    if grade_id:
        grade = db.get(Grade, int(grade_id)) if grade_id.isdigit() else None
        if grade is None:
            flash(request, "Unknown grade.", "error")
            return redirect(back)
    team = team.strip()
    expert = is_expert == "1"
    conflict = team_utils.expert_conflict(db, team, expert)
    if conflict:
        flash(request, conflict, "error")
        return redirect(back)

    existing = {u for u in db.scalars(select(User.username).where(User.username.in_(names)))}
    todo = [n for n in names if n not in existing]
    found = {}
    if lookup and todo:
        try:
            found = lookup_users(effective_settings(db, get_settings()), todo)
        except LdapUnavailable as exc:
            flash(request, f"Directory lookup failed: {exc}", "error")
            return redirect(back)

    created, problems, warnings = [], [f"{n}: already exists" for n in names if n in existing], []
    for name in todo:
        if lookup:
            profile, note = found[name]
            if profile is None:
                problems.append(f"{name}: {note}")
                continue
            if note:
                warnings.append(f"{profile.username}: {note}")
            if profile.manager is not None and manager is not None:
                profile.manager = None            # the manager chosen here wins; do not pull the directory one in
            if profile.username != name and db.scalar(select(User.id).where(User.username == profile.username)):
                problems.append(f"{name}: the directory account {profile.username} already exists")
                continue
            user = sync_user(db, profile, login=False)
        else:
            user = User(username=name, auth_source="ldap")
            db.add(user)
        if team:
            user.team, user.team_locked = team, True
        if manager is not None:
            user.manager_id, user.manager_locked = manager.id, True
        if is_expert in ("1", "0"):
            user.is_expert = expert
        if grade is not None:
            user.grade_id = grade.id
        db.flush()
        created.append(user.username)
    if created:
        audit(db, me, "user_preprovision", "user", None, usernames=created, lookup=bool(lookup), team=team,
              manager_id=manager.id if manager else None, expert=is_expert, grade=grade.code if grade else "")
        db.commit()
        flash(request, f"{len(created)} directory account(s) prepared: {', '.join(created[:8])}{'…' if len(created) > 8 else ''}. "
                       "They are completed from the directory at first sign-in.", "success")
    else:
        db.rollback()
    for msg in warnings[:5]:
        flash(request, f"Warning, {msg}", "info")
    for msg in problems[:6]:
        flash(request, f"Skipped {msg}", "error")
    return redirect("/admin/users" if created and not problems else back)


@router.get("/users/new")
def new_user_form(request: Request, db: Session = Depends(get_db)):
    return render(request, "admin/user_edit.html", _user_form_ctx(db, None))


@router.get("/users/{user_id}")
def edit_user_form(request: Request, user_id: int, db: Session = Depends(get_db)):
    user = db.get(User, user_id)
    if user is None:
        raise HTTPException(404, "Not found")
    return render(request, "admin/user_edit.html", _user_form_ctx(db, user))


@router.post("/users/save", dependencies=[Depends(verify_csrf)])
def save_user(
    request: Request,
    user_id: Optional[int] = Form(None), username: str = Form(""),
    first_name: str = Form(""), last_name: str = Form(""), display_name: str = Form(""),
    email: str = Form(""), employee_id: str = Form(""), team: str = Form(""),
    manager_id: Optional[str] = Form(None), manager_locked: Optional[str] = Form(None), team_locked: Optional[str] = Form(None),
    grade_id: Optional[str] = Form(None), is_expert: Optional[str] = Form(None),
    is_admin: Optional[str] = Form(None), is_procurement: Optional[str] = Form(None), is_active: Optional[str] = Form(None),
    password: str = Form(""),
    me: User = Depends(admin_user), db: Session = Depends(get_db),
):
    creating = user_id is None
    if creating:
        username = username.strip().lower()
        if not username:
            flash(request, "Username is required.", "error")
            return redirect("/admin/users/new")
        user = User(username=username, auth_source="local")
        db.add(user)
    else:
        user = db.get(User, user_id)
        if user is None:
            raise HTTPException(404, "Not found")
    back = "/admin/users/new" if creating else f"/admin/users/{user_id}"
    try:
        user.first_name, user.last_name = first_name.strip(), last_name.strip()
        user.display_name, user.email = display_name.strip(), email.strip()
        user.employee_id, user.team = employee_id.strip(), team.strip()
        user.grade_id = int(grade_id) if grade_id else None
        user.is_expert = bool(is_expert)
        user.manager_locked = bool(manager_locked)
        user.team_locked = bool(team_locked)
        conflict = team_utils.expert_conflict(db, user.team, user.is_expert)
        if conflict:
            raise ValueError(conflict)
        if user.id == me.id and (not is_admin or not is_active):
            raise ValueError("You cannot remove your own admin rights or deactivate yourself.")
        user.is_admin, user.is_active = bool(is_admin), bool(is_active) and not user.is_archived
        user.is_procurement = bool(is_procurement)
        new_manager = int(manager_id) if manager_id else None
        if new_manager is not None and user.id is not None:
            if new_manager == user.id or new_manager in subordinate_ids(db, user):
                raise ValueError("This manager would create a cycle in the hierarchy.")
        user.manager_id = new_manager
        if password:
            if user.auth_source != "local":
                raise ValueError("Passwords can only be set for local accounts.")
            check_password_policy(password)
            user.password_hash = hash_password(password)
        elif creating:
            raise ValueError("A password is required for a new local user.")
        db.flush()
        audit(db, me, "user_create" if creating else "user_update", "user", user.id,
              username=user.username, admin=user.is_admin, procurement=user.is_procurement, expert=user.is_expert, active=user.is_active,
              manager_id=user.manager_id, password_changed=bool(password))
        db.commit()
    except ValueError as exc:
        db.rollback()
        flash(request, str(exc), "error")
        return redirect(back)
    except IntegrityError:
        db.rollback()
        flash(request, "Username already exists.", "error")
        return redirect(back)
    flash(request, f"User {user.username} saved.", "success")
    return redirect("/admin/users")


@router.post("/users/bulk", dependencies=[Depends(verify_csrf)])
async def bulk_users(request: Request, me: User = Depends(admin_user), db: Session = Depends(get_db)):
    """Apply the filled-in fields to every selected user; a blank field means "no change". Users that would end up
    in an invalid state (hierarchy cycle, Expert in a team without on-call) are skipped and reported."""
    form = await request.form()
    back = str(form.get("back", "")) if str(form.get("back", "")).startswith("/admin/users") else "/admin/users"
    ids = sorted({int(x) for x in form.getlist("user_ids") if str(x).isdigit()})
    users_ = [u for u in (db.get(User, i) for i in ids) if u is not None]
    if not users_:
        flash(request, "Select at least one user.", "error")
        return redirect(back)

    team = str(form.get("team", "")).strip()
    clear_team = bool(form.get("clear_team"))
    manager_raw = str(form.get("manager_id", ""))
    expert_raw = str(form.get("is_expert", ""))
    grade_raw = str(form.get("grade_id", ""))
    lock = bool(form.get("lock"))
    if not (team or clear_team or manager_raw or expert_raw or grade_raw):
        flash(request, "Nothing to change: fill in at least one field.", "error")
        return redirect(back)

    new_manager = None
    if manager_raw not in ("", "__none__"):
        new_manager = db.get(User, int(manager_raw)) if manager_raw.isdigit() else None
        if new_manager is None or not new_manager.is_active or new_manager.is_archived:
            flash(request, "The chosen line manager is not an active user.", "error")
            return redirect(back)
    grade_id = int(grade_raw) if grade_raw.isdigit() else None
    if grade_raw.isdigit() and db.get(Grade, grade_id) is None:
        flash(request, "Unknown grade.", "error")
        return redirect(back)

    changed, skipped = 0, []
    for u in users_:
        try:
            if team or clear_team:
                u.team = "" if clear_team else team
                if u.auth_source == "ldap" and lock:
                    u.team_locked = True
            if expert_raw in ("1", "0"):
                u.is_expert = expert_raw == "1"
            conflict = team_utils.expert_conflict(db, u.team, u.is_expert)
            if conflict:
                raise ValueError(conflict)
            if grade_raw == "__default__":
                u.grade_id = None
            elif grade_id is not None:
                u.grade_id = grade_id
            if manager_raw == "__none__":
                u.manager_id = None
                if lock:
                    u.manager_locked = True
            elif new_manager is not None:
                if new_manager.id == u.id or new_manager.id in subordinate_ids(db, u):
                    raise ValueError(f"{new_manager.full_name} is below {u.full_name}: that would create a cycle in the hierarchy.")
                u.manager_id = new_manager.id
                if lock:
                    u.manager_locked = True
            db.flush()
            changed += 1
        except ValueError as exc:
            db.rollback()
            skipped.append(f"{u.username}: {exc}")
            # rolled back: re-apply nothing for this user; earlier users of the batch were flushed, so commit them first
            continue
        db.commit()
    audit(db, me, "users_bulk_update", "user", None, count=changed, skipped=len(skipped), team=team or ("" if not clear_team else "(cleared)"),
          manager=manager_raw, expert=expert_raw, grade=grade_raw, users=[u.username for u in users_])
    db.commit()
    if changed:
        flash(request, f"{changed} user(s) updated.", "success")
    for msg in skipped[:5]:
        flash(request, f"Skipped {msg}", "error")
    if len(skipped) > 5:
        flash(request, f"…and {len(skipped) - 5} more skipped.", "error")
    return redirect(back)


# ------------------------------------------------------------------ teams
def _team_overview(db: Session) -> List[dict]:
    rows = team_utils.team_rows(db)
    members: dict = {}
    for u in db.scalars(select(User).where(User.is_archived.is_not(True))):
        key = u.team.strip().lower()
        if key:
            members.setdefault(key, []).append(u)
    out = []
    for name in team_utils.all_team_names(db):
        key = name.lower()
        users_ = members.get(key, [])
        row = rows.get(key)
        # the line manager of most members: who to talk to about the team
        leads = {}
        for u in users_:
            if u.manager is not None:
                leads[u.manager.full_name] = leads.get(u.manager.full_name, 0) + 1
        out.append({
            "name": name, "members": len(users_), "experts": sum(1 for u in users_ if u.is_expert),
            "oncall": row is None or row.oncall_enabled,
            "managers": sorted(leads, key=lambda n: -leads[n])[:3],
        })
    return out


@router.get("/teams")
def teams_page(request: Request, db: Session = Depends(get_db)):
    no_team = db.scalar(select(func.count(User.id)).where(User.is_archived.is_not(True), func.trim(User.team) == ""))
    return render(request, "admin/teams.html", {"teams": _team_overview(db), "no_team": no_team, "no_team_key": NO_TEAM})


def _team_row(db: Session, name: str) -> Optional[Team]:
    return team_utils.team_rows(db).get(name.strip().lower())


@router.post("/teams/create", dependencies=[Depends(verify_csrf)])
def create_team(request: Request, name: str = Form(""), oncall: Optional[str] = Form(None),
                me: User = Depends(admin_user), db: Session = Depends(get_db)):
    name = name.strip()
    if not name:
        flash(request, "Team name is required.", "error")
    elif name.lower() in {n.lower() for n in team_utils.all_team_names(db, include_archived=True)}:
        flash(request, f"The team {name} already exists.", "error")
    else:
        db.add(Team(name=name, oncall_enabled=bool(oncall)))
        audit(db, me, "team_create", "team", None, name=name, oncall=bool(oncall))
        db.commit()
        flash(request, f"Team {name} created. Assign members from Users (bulk edit) or the user form.", "success")
    return redirect("/admin/teams")


@router.post("/teams/save", dependencies=[Depends(verify_csrf)])
def save_team(request: Request, name: str = Form(""), new_name: str = Form(""), oncall: Optional[str] = Form(None),
              me: User = Depends(admin_user), db: Session = Depends(get_db)):
    """Rename a team (updates every member) and/or switch on-call on or off for it."""
    new_name = new_name.strip() or name
    enabled = bool(oncall)
    row = _team_row(db, name)
    members = list(db.scalars(select(User).where(func.lower(User.team) == name.strip().lower())))
    if not members and row is None:
        flash(request, "Unknown team.", "error")
        return redirect("/admin/teams")
    others = {n.lower() for n in team_utils.all_team_names(db, include_archived=True)} - {name.strip().lower()}
    if new_name.lower() in others:
        flash(request, f"A team named {new_name} already exists.", "error")
        return redirect("/admin/teams")
    experts = [u for u in members if u.is_expert and not u.is_archived]
    if not enabled and experts:
        flash(request, f"On-call cannot be switched off for {name}: {len(experts)} Expert(s) belong to it and Experts only declare "
                       "on-call. Move them to another team or clear their Expert flag first.", "error")
        return redirect("/admin/teams")
    was = row is None or row.oncall_enabled
    if row is None:
        row = Team(name=new_name, oncall_enabled=enabled)
        db.add(row)
    row.name, row.oncall_enabled = new_name, enabled
    for u in members:
        u.team = new_name
        if u.auth_source == "ldap" and new_name != name:
            u.team_locked = True   # the directory still says the old name
    audit(db, me, "team_update", "team", row.id, name=name, new_name=new_name, oncall=enabled, oncall_was=was, members=len(members))
    db.commit()
    parts = []
    if new_name != name:
        parts.append(f"renamed to {new_name}")
    if enabled != was:
        parts.append("on-call switched " + ("on" if enabled else "off") +
                     ("" if enabled else ": existing on-call sheets stay visible, drafts can no longer be edited or submitted"))
    flash(request, f"Team {name}: " + ("; ".join(parts) if parts else "no change") + ".", "success" if parts else "info")
    return redirect("/admin/teams")


@router.post("/teams/delete", dependencies=[Depends(verify_csrf)])
def delete_team(request: Request, name: str = Form(""), me: User = Depends(admin_user), db: Session = Depends(get_db)):
    count = db.scalar(select(func.count(User.id)).where(func.lower(User.team) == name.strip().lower()))
    if count:
        flash(request, f"{name} still has {count} member(s) (archived users included): move them first.", "error")
        return redirect("/admin/teams")
    row = _team_row(db, name)
    if row is not None:
        db.delete(row)
        audit(db, me, "team_delete", "team", row.id, name=row.name)
        db.commit()
        flash(request, f"Team {name} deleted.", "success")
    return redirect("/admin/teams")


# ------------------------------------------------------------------ grades
@router.get("/grades")
def grades(request: Request, db: Session = Depends(get_db)):
    return render(request, "admin/grades.html", {"grades": list(db.scalars(select(Grade).order_by(Grade.code)))})


@router.post("/grades", dependencies=[Depends(verify_csrf)])
def save_grade(request: Request, grade_id: Optional[int] = Form(None), code: str = Form(""), name: str = Form(""),
               rate_weekday: str = Form("0"), rate_saturday: str = Form("0"), rate_sunday: str = Form("0"),
               rate_holiday: str = Form("0"), is_default: Optional[str] = Form(None),
               me: User = Depends(admin_user), db: Session = Depends(get_db)):
    try:
        grade = db.get(Grade, grade_id) if grade_id else Grade()
        if grade is None:
            raise HTTPException(404, "Not found")
        if not code.strip():
            raise ValueError("Code is required.")
        grade.code, grade.name = code.strip().upper(), name.strip() or code.strip()
        grade.rate_weekday = _dec(rate_weekday, "Weekday rate")
        grade.rate_saturday = _dec(rate_saturday, "Saturday rate")
        grade.rate_sunday = _dec(rate_sunday, "Sunday rate")
        grade.rate_holiday = _dec(rate_holiday, "Holiday rate")
        if is_default:
            for g in db.scalars(select(Grade)):
                g.is_default = False
        grade.is_default = bool(is_default) or bool(grade.is_default)
        if grade.id is None:
            db.add(grade)
        db.flush()
        audit(db, me, "grade_save", "grade", grade.id, code=grade.code, rates=[
            str(grade.rate_weekday), str(grade.rate_saturday), str(grade.rate_sunday), str(grade.rate_holiday)])
        db.commit()
        flash(request, f"Grade {grade.code} saved. Rates apply to sheets saved or submitted from now on.", "success")
    except ValueError as exc:
        db.rollback()
        flash(request, str(exc), "error")
    except IntegrityError:
        db.rollback()
        flash(request, "Grade code already exists.", "error")
    return redirect("/admin/grades")


def _delete_blocker(db: Session, me: User, user: User) -> Optional[str]:
    """Why this user cannot be deleted (None when deletion is allowed). Approved sheets and payroll runs are
    accounting evidence, so those users can only be deactivated."""
    if user.id == me.id:
        return "You cannot delete your own account."
    if user.is_admin and user.is_active and db.scalar(
            select(func.count(User.id)).where(User.is_admin.is_(True), User.is_active.is_(True), User.id != user.id)) == 0:
        return "This is the last active administrator."
    if db.scalar(select(func.count(Sheet.id)).where(Sheet.user_id == user.id, Sheet.status == APPROVED)):
        return "This user has approved sheets (payroll records). Archive the account instead."
    if db.scalar(select(func.count(PayrollRun.id)).where(PayrollRun.sent_by_id == user.id)):
        return "This user recorded a payroll run sent to Finance. Archive the account instead."
    return None


@router.post("/users/{user_id}/delete", dependencies=[Depends(verify_csrf)])
def delete_user(request: Request, user_id: int, me: User = Depends(admin_user), db: Session = Depends(get_db)):
    user = db.get(User, user_id)
    if user is None:
        raise HTTPException(404, "Not found")
    blocker = _delete_blocker(db, me, user)
    if blocker:
        flash(request, blocker, "error")
        return redirect(f"/admin/users/{user_id}")
    name = user.username
    sheet_ids = list(db.scalars(select(Sheet.id).where(Sheet.user_id == user_id)))
    reports = db.execute(update(User).where(User.manager_id == user_id).values(manager_id=None)).rowcount
    db.execute(update(Sheet).where(Sheet.decided_by_id == user_id).values(decided_by_id=None))
    db.execute(update(AuditLog).where(AuditLog.actor_id == user_id).values(actor_id=None))
    db.execute(delete(Signature).where(or_(Signature.user_id == user_id, Signature.sheet_id.in_(sheet_ids))))
    db.execute(delete(SavedSignature).where(SavedSignature.user_id == user_id))
    db.execute(delete(Notification).where(Notification.user_id == user_id))
    db.execute(delete(OnCallDay).where(OnCallDay.sheet_id.in_(sheet_ids)))
    db.execute(delete(OvertimeEntry).where(OvertimeEntry.sheet_id.in_(sheet_ids)))
    db.execute(delete(Sheet).where(Sheet.user_id == user_id))
    db.expire_all()
    db.delete(db.get(User, user_id))
    audit(db, me, "user_delete", "user", user_id, username=name, sheets_removed=len(sheet_ids), reports_unlinked=reports)
    db.commit()
    flash(request, f"User {name} deleted" + (f"; {reports} direct report(s) now have no manager." if reports else "."), "success")
    return redirect("/admin/users")


def _archive_blocker(db: Session, me: User, user: User) -> Optional[str]:
    if user.id == me.id:
        return "You cannot archive your own account."
    if user.is_admin and user.is_active and db.scalar(
            select(func.count(User.id)).where(User.is_admin.is_(True), User.is_active.is_(True), User.id != user.id)) == 0:
        return "This is the last active administrator."
    return None


@router.post("/users/{user_id}/archive", dependencies=[Depends(verify_csrf)])
def archive_user(request: Request, user_id: int, me: User = Depends(admin_user), db: Session = Depends(get_db)):
    user = db.get(User, user_id)
    if user is None:
        raise HTTPException(404, "Not found")
    blocker = _archive_blocker(db, me, user)
    if blocker:
        flash(request, blocker, "error")
        return redirect(f"/admin/users/{user_id}")
    user.is_active, user.is_archived = False, True
    live_reports = db.scalar(select(func.count(User.id)).where(User.manager_id == user_id, User.is_active.is_(True)))
    audit(db, me, "user_archive", "user", user.id, username=user.username, active_reports=live_reports)
    db.commit()
    msg = f"User {user.username} archived: sign-in disabled, records kept."
    if live_reports:
        msg += f" {live_reports} active direct report(s) still point to this manager; assign them a new one."
    flash(request, msg, "success")
    return redirect("/admin/users")


@router.post("/users/{user_id}/unarchive", dependencies=[Depends(verify_csrf)])
def unarchive_user(request: Request, user_id: int, me: User = Depends(admin_user), db: Session = Depends(get_db)):
    user = db.get(User, user_id)
    if user is None:
        raise HTTPException(404, "Not found")
    user.is_active, user.is_archived = True, False
    audit(db, me, "user_unarchive", "user", user.id, username=user.username)
    db.commit()
    flash(request, f"User {user.username} restored and active again.", "success")
    return redirect(f"/admin/users/{user_id}")


# ------------------------------------------------------------------ holidays
@router.get("/docs")
def docs(request: Request, db: Session = Depends(get_db)):
    day, handover = oncall_week(db, get_settings())
    names = backups.WEEKDAYS
    return render(request, "admin/docs.html", {
        "week_start_name": names[day], "week_end_name": names[(day + 6) % 7], "week_handover": handover,
        "grades": list(db.scalars(select(Grade).order_by(Grade.code))),
        "rules": {r.day_type: r for r in db.scalars(select(OvertimeRule))},
        "til_on": get_flag(db, SETTING_TIL_ENABLED),
    })


@router.get("/holidays")
def holidays(request: Request, db: Session = Depends(get_db)):
    return render(request, "admin/holidays.html", {"holidays": list(db.scalars(select(Holiday).order_by(Holiday.date)))})


@router.post("/holidays", dependencies=[Depends(verify_csrf)])
def add_holiday(request: Request, date: str = Form(...), name: str = Form(""),
                me: User = Depends(admin_user), db: Session = Depends(get_db)):
    try:
        h = Holiday(date=dt.date.fromisoformat(date), name=name.strip() or "Holiday")
        db.add(h)
        db.flush()
        audit(db, me, "holiday_add", "holiday", h.id, date=date, name=h.name)
        db.commit()
        flash(request, "Holiday added.", "success")
    except ValueError:
        db.rollback()
        flash(request, "Invalid date.", "error")
    except IntegrityError:
        db.rollback()
        flash(request, "This date is already a holiday.", "error")
    return redirect("/admin/holidays")


@router.post("/holidays/import", dependencies=[Depends(verify_csrf)])
async def import_holidays(request: Request, file: UploadFile = File(...), mode: str = Form("merge"),
                          me: User = Depends(admin_user), db: Session = Depends(get_db)):
    """Import public holidays from an .ics file. merge = add/rename by date; replace = the years
    contained in the file are replaced entirely (dates not in the file are removed for those years)."""
    try:
        result = parse_ics(await file.read(MAX_BYTES + 1))
    except IcsError as exc:
        flash(request, f"Import failed: {exc}", "error")
        return redirect("/admin/holidays")
    removed = 0
    if mode == "replace" and result.years:
        for h in db.scalars(select(Holiday).where(extract("year", Holiday.date).in_(result.years))):
            if h.date not in result.holidays:
                db.delete(h)
                removed += 1
    existing = {h.date: h for h in db.scalars(select(Holiday).where(Holiday.date.in_(list(result.holidays))))}
    added = renamed = 0
    for date, name in sorted(result.holidays.items()):
        h = existing.get(date)
        if h is None:
            db.add(Holiday(date=date, name=name))
            added += 1
        elif h.name != name:
            h.name = name
            renamed += 1
    audit(db, me, "holiday_import", "holiday", None, file=file.filename, mode=mode, years=result.years,
          added=added, renamed=renamed, removed=removed, skipped_recurring=result.skipped_recurring)
    db.commit()
    msg = (f"Imported {len(result.holidays)} holiday date(s) for {', '.join(map(str, result.years)) or '-'}: "
           f"{added} added, {renamed} renamed, {removed} removed.")
    if result.skipped_recurring or result.skipped_other:
        msg += (f" Skipped {result.skipped_recurring} recurring and {result.skipped_other} other event(s) "
                "(cancelled, too long or invalid).")
    flash(request, msg + " Draft sheets pick up the new holidays at their next save; submitted sheets keep "
                         "their snapshot.", "success")
    return redirect("/admin/holidays")


@router.post("/holidays/{holiday_id}/delete", dependencies=[Depends(verify_csrf)])
def delete_holiday(request: Request, holiday_id: int, me: User = Depends(admin_user), db: Session = Depends(get_db)):
    h = db.get(Holiday, holiday_id)
    if h is not None:
        audit(db, me, "holiday_delete", "holiday", h.id, date=h.date, name=h.name)
        db.delete(h)
        db.commit()
        flash(request, "Holiday deleted.", "success")
    return redirect("/admin/holidays")


# ------------------------------------------------------------------ overtime rules
@router.get("/rules")
def rules(request: Request, db: Session = Depends(get_db)):
    by_type = {r.day_type: r for r in db.scalars(select(OvertimeRule))}
    return render(request, "admin/rules.html", {"rules": [by_type[t] for t in DAY_TYPES if t in by_type]})


@router.post("/rules", dependencies=[Depends(verify_csrf)])
async def save_rules(request: Request, me: User = Depends(admin_user), db: Session = Depends(get_db)):
    form = await request.form()
    try:
        for rule in db.scalars(select(OvertimeRule)):
            rule.til_factor = _dec(str(form.get(f"day_{rule.day_type}", "")), f"{rule.day_type} day factor")
            rule.til_factor_night = _dec(str(form.get(f"night_{rule.day_type}", "")), f"{rule.day_type} night factor")
        audit(db, me, "rules_update", "overtime_rules", None,
              **{k: str(v) for k, v in form.items() if k != "csrf_token"})
        db.commit()
        flash(request, "TIL factors saved.", "success")
    except ValueError as exc:
        db.rollback()
        flash(request, str(exc), "error")
    return redirect("/admin/rules")


# ------------------------------------------------------------------ settings
def _settings_ctx(db: Session, **extra) -> dict:
    today = dt.date.today()
    base = get_settings()
    return {
        "show_amounts": get_flag(db, SETTING_SHOW_AMOUNTS),
        "til_enabled": get_flag(db, SETTING_TIL_ENABLED),
        "signature_required": get_flag(db, SETTING_SIGNATURE_REQUIRED),
        "week_start": oncall_week(db, base)[0], "handover": oncall_week(db, base)[1],
        "weekdays": backups.WEEKDAYS,
        "cutoff_schedule": cutoff_schedule(db, base, today.year, today.month, 6),
        "ldap": effective_settings(db, base),
        "ldap_custom": set(ldap_overrides(db)),
        "secret_status": secret_status(db, base),
        **extra,
    }


@router.get("/settings")
def settings_page(request: Request, db: Session = Depends(get_db)):
    return render(request, "admin/settings.html", _settings_ctx(db))


def _parse_ldap_form(form) -> dict:
    """Validate the LDAP form. Raises ValueError with a user-facing message."""
    values = {}
    for attr, kind in LDAP_FIELDS.items():
        raw = form.get(attr)
        if kind is bool:
            values[attr] = bool(raw)
        elif kind is int:
            try:
                values[attr] = int(str(raw or "").strip())
            except ValueError:
                raise ValueError("Timeout must be a number of seconds.")
        else:
            values[attr] = str(raw or "").replace("\r\n", "\n").strip()
    url = values["ldap_url"].lower()
    if values["ldap_enabled"] and not url:
        raise ValueError("The LDAP URL is required when LDAP authentication is enabled.")
    if url and not (url.startswith("ldap://") or url.startswith("ldaps://")):
        raise ValueError("The LDAP URL must start with ldap:// or ldaps://.")
    if url.startswith("ldaps://") and values["ldap_start_tls"]:
        raise ValueError("Use either ldaps:// or StartTLS (with ldap://), not both.")
    if "{username}" not in values["ldap_user_filter"]:
        raise ValueError("The user filter must contain the {username} placeholder.")
    if not 1 <= values["ldap_timeout"] <= 120:
        raise ValueError("Timeout must be between 1 and 120 seconds.")
    pem = values["ldap_ca_cert_data"]
    if pem and "-----BEGIN CERTIFICATE-----" not in pem:
        raise ValueError("The CA certificate must be in PEM format (-----BEGIN CERTIFICATE-----).")
    for attr in ("ldap_attr_username", "ldap_user_base_dn"):
        if values["ldap_enabled"] and not values[attr]:
            raise ValueError("User base DN and username attribute are required.")
    return values


@router.post("/settings/ldap", dependencies=[Depends(verify_csrf)])
async def save_ldap(request: Request, me: User = Depends(admin_user), db: Session = Depends(get_db)):
    form = await request.form()
    try:
        values = _parse_ldap_form(form)
    except ValueError as exc:
        flash(request, str(exc), "error")
        return redirect("/admin/settings#ldap")
    save_ldap_settings(db, get_settings(), values)
    if form.get("clear_bind_password"):
        row = db.get(AppSetting, LDAP_PREFIX + "bind_password")
        if row is not None:
            db.delete(row)
    audit(db, me, "ldap_settings_update", "settings", None,
          **{k: v for k, v in values.items() if k not in SECRET_FIELDS and k != "ldap_ca_cert_data"},
          password_changed=bool(values["ldap_bind_password"]), ca_cert_set=bool(values["ldap_ca_cert_data"]))
    db.commit()
    flash(request, "LDAP settings saved - they apply to the next login. Use \"Test connection\" to check them.",
          "success")
    return redirect("/admin/settings#ldap")


@router.post("/settings/ldap/reset", dependencies=[Depends(verify_csrf)])
def reset_ldap(request: Request, me: User = Depends(admin_user), db: Session = Depends(get_db)):
    n = clear_ldap_settings(db)
    audit(db, me, "ldap_settings_reset", "settings", None, removed=n)
    db.commit()
    flash(request, "LDAP settings reset to the environment (.env) values.", "success")
    return redirect("/admin/settings#ldap")


@router.post("/settings/ldap/test", dependencies=[Depends(verify_csrf)])
def test_ldap(request: Request, test_username: str = Form(""), me: User = Depends(admin_user),
              db: Session = Depends(get_db)):
    result = diagnose(effective_settings(db, get_settings()), test_username.strip())
    audit(db, me, "ldap_test", "settings", None, username=test_username.strip(), ok=result.ok)
    db.commit()
    return render(request, "admin/settings.html", _settings_ctx(db, diagnosis=result, test_username=test_username))


@router.post("/settings", dependencies=[Depends(verify_csrf)])
def save_settings(request: Request, show_amounts: Optional[str] = Form(None), til_enabled: Optional[str] = Form(None),
                  signature_required: Optional[str] = Form(None), week_start: Optional[int] = Form(None),
                  handover: str = Form(""), payroll_cutoff: Optional[int] = Form(None), me: User = Depends(admin_user), db: Session = Depends(get_db)):
    set_setting(db, SETTING_SHOW_AMOUNTS, "1" if show_amounts else "0")
    set_setting(db, SETTING_TIL_ENABLED, "1" if til_enabled else "0")
    set_setting(db, SETTING_SIGNATURE_REQUIRED, "1" if signature_required else "0")
    if payroll_cutoff is not None:
        if not 1 <= payroll_cutoff <= 28:
            flash(request, "The cut-off day must be between 1 and 28.", "error")
            return redirect("/admin/settings")
        set_setting(db, SETTING_PAYROLL_CUTOFF, str(payroll_cutoff))
    if week_start is not None:
        try:
            parse_hhmm(handover)
        except ValueError:
            flash(request, "Invalid handover time.", "error")
            return redirect("/admin/settings")
        if not 0 <= week_start <= 6:
            flash(request, "Invalid week start day.", "error")
            return redirect("/admin/settings")
        set_setting(db, SETTING_ONCALL_WEEK_START, str(week_start))
        set_setting(db, SETTING_ONCALL_HANDOVER, handover)
    audit(db, me, "settings_update", "settings", None, show_amounts_to_employees=bool(show_amounts),
          til_enabled=bool(til_enabled), signature_required=bool(signature_required),
          oncall_week_start=week_start, oncall_handover=handover, payroll_cutoff=payroll_cutoff)
    db.commit()
    flash(request, "Settings saved.", "success")
    return redirect("/admin/settings")


# ------------------------------------------------------------------ audit
@router.get("/audit")
def audit_log(request: Request, db: Session = Depends(get_db)):
    rows = list(db.scalars(select(AuditLog).order_by(AuditLog.id.desc()).limit(500)))
    return render(request, "admin/audit.html", {"rows": rows})


# ------------------------------------------------------------------ backup
def _backup_ctx(db: Session, **extra) -> dict:
    s = get_settings()
    sch = backups.load_schedule(db)
    now = utcnow()
    return {"files": backups.list_backups(s.backup_dir), "backup_dir": s.backup_dir, "sch": sch,
            "next_run": backups.next_occurrence(now, sch) if sch.enabled else None,
            "weekdays": backups.WEEKDAYS, "database": db.get_bind().dialect.name,
            "key_source": "BACKUP_PASSPHRASE" if s.backup_passphrase else "SECRET_KEY", **extra}


@router.get("/backup")
def backup_page(request: Request, db: Session = Depends(get_db)):
    return render(request, "admin/backup.html", _backup_ctx(db))


@router.post("/backup/schedule", dependencies=[Depends(verify_csrf)])
def backup_schedule(request: Request, enabled: Optional[str] = Form(None), frequency: str = Form("daily"),
                    weekday: int = Form(0), time: str = Form("02:00"), keep: int = Form(14),
                    me: User = Depends(admin_user), db: Session = Depends(get_db)):
    try:
        minute = parse_hhmm(time)
    except ValueError:
        minute = -1
    if frequency not in ("daily", "weekly") or not 0 <= weekday <= 6 or not 0 <= minute < 1440 or not 1 <= keep <= 365:
        flash(request, "Invalid schedule: check the frequency, time and the number of backups to keep (1-365).", "error")
        return redirect("/admin/backup")
    sch = backups.Schedule(enabled=bool(enabled), frequency=frequency, weekday=weekday, minute=minute, keep=keep)
    backups.save_schedule(db, sch)
    audit(db, me, "backup_schedule", "backup", None, enabled=sch.enabled, frequency=frequency, weekday=weekday,
          time=time, keep=keep)
    db.commit()
    flash(request, "Backup schedule saved." if sch.enabled else "Scheduled backups are switched off.", "success")
    return redirect("/admin/backup")


@router.post("/backup/create", dependencies=[Depends(verify_csrf)])
def backup_create(request: Request, me: User = Depends(admin_user), db: Session = Depends(get_db)):
    s = get_settings()
    try:
        made = backups.create_backup(get_engine(), s.backup_dir, backups.passphrase_of(s), "manual")
    except OSError as exc:
        flash(request, f"The backup could not be written: {exc.strerror or exc}.", "error")
        return redirect("/admin/backup")
    audit(db, me, "backup_create", "backup", None, name=made.name, size=made.size)
    db.commit()
    flash(request, f"Backup {made.name} created (encrypted).", "success")
    return redirect("/admin/backup")


@router.get("/backup/download/{name}")
def backup_download(name: str, me: User = Depends(admin_user), db: Session = Depends(get_db)):
    try:
        path = backups.backup_path(get_settings().backup_dir, name)
    except backups.BackupError:
        raise HTTPException(status_code=404, detail="Not found")
    audit(db, me, "backup_download", "backup", None, name=name)
    db.commit()
    return FileResponse(path, media_type="application/octet-stream", filename=name, headers={"Cache-Control": "no-store"})


@router.post("/backup/upload", dependencies=[Depends(verify_csrf)])
async def backup_upload(request: Request, file: UploadFile = File(...), source_passphrase: str = Form(""),
                        me: User = Depends(admin_user), db: Session = Depends(get_db)):
    s = get_settings()
    data = await file.read(backups.MAX_UPLOAD_BYTES + 1)
    if len(data) > backups.MAX_UPLOAD_BYTES:
        flash(request, "This file is too large (200 MB maximum).", "error")
        return redirect("/admin/backup")
    try:
        name = backups.store_uploaded(s.backup_dir, data, source_passphrase or backups.passphrase_of(s),
                                      backups.passphrase_of(s))
    except backups.BackupError as exc:
        flash(request, str(exc), "error")
        return redirect("/admin/backup")
    audit(db, me, "backup_upload", "backup", None, name=name, size=len(data))
    db.commit()
    flash(request, f"Backup uploaded as {name}. Restore it from the list below if you want to use it.", "success")
    return redirect("/admin/backup")


@router.post("/backup/delete/{name}", dependencies=[Depends(verify_csrf)])
def backup_delete(request: Request, name: str, me: User = Depends(admin_user), db: Session = Depends(get_db)):
    try:
        backups.delete_backup(get_settings().backup_dir, name)
    except backups.BackupError as exc:
        flash(request, str(exc), "error")
        return redirect("/admin/backup")
    audit(db, me, "backup_delete", "backup", None, name=name)
    db.commit()
    flash(request, f"Backup {name} deleted.", "success")
    return redirect("/admin/backup")


@router.post("/backup/restore/{name}", dependencies=[Depends(verify_csrf)])
def backup_restore(request: Request, name: str, confirm: str = Form(""), me: User = Depends(admin_user),
                   db: Session = Depends(get_db)):
    if confirm.strip() != "RESTORE":
        flash(request, "Type RESTORE to confirm: the whole database is replaced by the backup.", "error")
        return redirect("/admin/backup")
    s = get_settings()
    try:
        info = backups.restore_backup(get_engine(), s.backup_dir, name, backups.passphrase_of(s))
    except backups.BackupError as exc:
        flash(request, str(exc), "error")
        return redirect("/admin/backup")
    except Exception as exc:  # the transaction was rolled back: the database is unchanged
        logging.getLogger(__name__).exception("Backup restore failed")
        db.rollback()
        flash(request, f"Restore failed, the database was left unchanged ({type(exc).__name__}).", "error")
        return redirect("/admin/backup")
    db.rollback()
    with Session(get_engine()) as fresh:
        audit(fresh, None, "backup_restore", "backup", None, name=name, restored_by=me.username,
              created=info["created"], app_version=info["app_version"])
        fresh.commit()
    request.session.clear()  # the signed-in account may not exist in the backup
    flash(request, f"Database restored from {name}. A safety copy of the previous state was saved first. "
                   "Sign in again: the accounts are those of the backup.", "success")
    return redirect("/login")
