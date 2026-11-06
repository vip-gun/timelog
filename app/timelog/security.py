"""Password hashing, session handling, CSRF and current-user dependencies."""
import base64
import hashlib
import hmac
import secrets
import threading
import time
from typing import Dict, List, Optional, Tuple

from fastapi import Depends, HTTPException, Request
from sqlalchemy.orm import Session

from .db import get_db
from .models import User

# ---------------------------------------------------------------- passwords (PBKDF2-SHA256)
# PBKDF2 is available in every Python build (scrypt is missing when Python is linked to LibreSSL).
_PBKDF2_ITERATIONS = 600_000


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, _PBKDF2_ITERATIONS)
    return "pbkdf2_sha256${}${}${}".format(
        _PBKDF2_ITERATIONS, base64.b64encode(salt).decode(), base64.b64encode(dk).decode())


def verify_password(password: str, stored: Optional[str]) -> bool:
    if not stored or not password:
        return False
    try:
        algo, iterations, salt_b64, dk_b64 = stored.split("$")
        if algo != "pbkdf2_sha256":
            return False
        salt, expected = base64.b64decode(salt_b64), base64.b64decode(dk_b64)
        dk = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, int(iterations))
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(dk, expected)


MIN_PASSWORD_LENGTH = 10


def check_password_policy(password: str) -> None:
    if len(password) < MIN_PASSWORD_LENGTH:
        raise ValueError(f"Password must be at least {MIN_PASSWORD_LENGTH} characters.")


# ---------------------------------------------------------------- login throttling
class LoginThrottle:
    """In-memory, per-process throttle: MAX_FAILURES within WINDOW seconds per (username, ip)."""

    MAX_FAILURES = 5
    WINDOW = 15 * 60

    def __init__(self) -> None:
        self._failures: Dict[Tuple[str, str], List[float]] = {}
        self._lock = threading.Lock()

    def _recent(self, key: Tuple[str, str]) -> List[float]:
        now = time.monotonic()
        items = [t for t in self._failures.get(key, []) if now - t < self.WINDOW]
        self._failures[key] = items
        return items

    def blocked(self, username: str, ip: str) -> bool:
        with self._lock:
            return len(self._recent((username, ip))) >= self.MAX_FAILURES

    def fail(self, username: str, ip: str) -> None:
        with self._lock:
            self._recent((username, ip)).append(time.monotonic())

    def reset(self, username: str, ip: str) -> None:
        with self._lock:
            self._failures.pop((username, ip), None)


throttle = LoginThrottle()


# ---------------------------------------------------------------- session / csrf
def csrf_token(request: Request) -> str:
    token = request.session.get("csrf")
    if not token:
        token = secrets.token_urlsafe(32)
        request.session["csrf"] = token
    return token


async def verify_csrf(request: Request) -> None:
    form = await request.form()
    sent = form.get("csrf_token") or request.headers.get("x-csrf-token") or ""
    expected = request.session.get("csrf") or ""
    if not expected or not hmac.compare_digest(str(sent), expected):
        raise HTTPException(status_code=400, detail="Invalid or missing CSRF token. Reload the page and retry.")


def login_session(request: Request, user: User) -> None:
    request.session.clear()
    request.session["uid"] = user.id
    request.session["csrf"] = secrets.token_urlsafe(32)


def flash(request: Request, message: str, category: str = "info") -> None:
    request.session.setdefault("flash", [])
    request.session["flash"] = request.session["flash"] + [[category, message]]


def pop_flashes(request: Request) -> list:
    return request.session.pop("flash", [])


class LoginRequired(Exception):
    pass


def current_user(request: Request, db: Session = Depends(get_db)) -> User:
    uid = request.session.get("uid")
    user = db.get(User, uid) if uid else None
    if user is None or not user.is_active:
        request.session.pop("uid", None)
        raise LoginRequired()
    from .access import is_manager
    from .app_settings import features

    request.state.user = user
    request.state.is_manager = is_manager(db, user)
    request.state.features = features(db)
    from .teams import oncall_enabled
    request.state.oncall_enabled = oncall_enabled(db, user)
    from sqlalchemy import select

    from .config import get_settings
    from .app_settings import payroll_cutoff
    from .models import Notification

    request.state.payroll_cutoff = payroll_cutoff(db, get_settings())
    request.state.notices = list(db.scalars(select(Notification).where(
        Notification.user_id == user.id, Notification.read_at.is_(None)).order_by(Notification.id.desc()).limit(6)))
    return user


def payroll_user(user: User = Depends(current_user)) -> User:
    """IT Procurement (or an administrator): payroll reports and the "sent to Finance" record."""
    if not (user.is_procurement or user.is_admin):
        raise HTTPException(status_code=404, detail="Not found")
    return user


def admin_user(user: User = Depends(current_user)) -> User:
    if not user.is_admin:
        raise HTTPException(status_code=404, detail="Not found")
    return user
