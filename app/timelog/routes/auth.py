import logging

from fastapi import APIRouter, Depends, Form, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..app_settings import effective_settings
from ..config import get_settings
from ..db import get_db
from ..ldap_auth import LdapUnavailable, authenticate, sync_user
from ..models import User, utcnow
from ..security import login_session, throttle, verify_csrf, verify_password
from ..web import redirect, render
from ..workflow import audit

log = logging.getLogger(__name__)
router = APIRouter()


def _safe_next(target: str) -> str:
    # Only allow local paths to prevent open redirects.
    if target and target.startswith("/") and not target.startswith("//") and "\\" not in target:
        return target
    return "/"


@router.get("/login")
def login_form(request: Request, next: str = "", db: Session = Depends(get_db)):
    if request.session.get("uid"):
        return redirect(_safe_next(next))
    s = effective_settings(db, get_settings())
    return render(request, "login.html", {"next": next, "ldap_enabled": s.ldap_enabled})


@router.post("/login", dependencies=[Depends(verify_csrf)])
def login(request: Request, username: str = Form(""), password: str = Form(""), next: str = Form(""),
          db: Session = Depends(get_db)):
    s = effective_settings(db, get_settings())  # LDAP values edited in Admin > Settings
    username = username.strip().lower()
    ip = request.client.host if request.client else "?"
    ctx = {"next": next, "ldap_enabled": s.ldap_enabled, "username": username}

    if throttle.blocked(username, ip):
        ctx["error"] = "Too many failed attempts. Please wait 15 minutes."
        return render(request, "login.html", ctx, 429)

    user = db.scalar(select(User).where(User.username == username)) if username else None
    authenticated = None
    try:
        if user is not None and user.auth_source == "local":
            allowed = s.local_login_enabled and (user.is_admin or not s.local_login_admin_only)
            if allowed and verify_password(password, user.password_hash):
                authenticated = user
        elif s.ldap_enabled and username:
            profile = authenticate(s, username, password)
            if profile is not None:
                authenticated = sync_user(db, profile)
    except LdapUnavailable:
        log.exception("LDAP unavailable")
        db.rollback()
        ctx["error"] = "The directory service is unavailable. Please try again later."
        return render(request, "login.html", ctx, 503)

    if authenticated is None or not authenticated.is_active:
        db.rollback()
        throttle.fail(username, ip)
        log.info("Failed login for %r from %s", username, ip)
        ctx["error"] = "Invalid username or password."
        return render(request, "login.html", ctx, 401)

    throttle.reset(username, ip)
    authenticated.last_login_at = utcnow()
    audit(db, authenticated, "login", "user", authenticated.id, ip=ip, source=authenticated.auth_source)
    db.commit()
    login_session(request, authenticated)
    return redirect(_safe_next(next))


@router.post("/logout", dependencies=[Depends(verify_csrf)])
def logout(request: Request):
    request.session.clear()
    return redirect("/login")
