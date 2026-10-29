"""Administrator-editable runtime settings stored in the `app_settings` table.

LDAP configuration can be edited in Admin > Settings. A value stored in the database overrides the
corresponding environment variable; deleting the rows ("reset") falls back to the environment.
Secrets are encrypted with a key derived from SECRET_KEY and are never sent back to the browser.
"""
import base64
import dataclasses
import hashlib
from typing import Dict, Optional

from cryptography.fernet import Fernet, InvalidToken
from sqlalchemy import select
from sqlalchemy.orm import Session

from .config import Settings
from .models import (
    SETTING_DEFAULTS, SETTING_SHOW_AMOUNTS, SETTING_SIGNATURE_REQUIRED, SETTING_TIL_ENABLED, AppSetting, User,
)

LDAP_PREFIX = "ldap."
# Settings attribute -> type, editable from the UI
LDAP_FIELDS = {
    "ldap_enabled": bool,
    "ldap_url": str,
    "ldap_start_tls": bool,
    "ldap_verify_cert": bool,
    "ldap_ca_cert_data": str,
    "ldap_bind_dn": str,
    "ldap_bind_password": str,
    "ldap_user_base_dn": str,
    "ldap_user_filter": str,
    "ldap_attr_username": str,
    "ldap_attr_first_name": str,
    "ldap_attr_last_name": str,
    "ldap_attr_display_name": str,
    "ldap_attr_email": str,
    "ldap_attr_employee_id": str,
    "ldap_attr_team": str,
    "ldap_attr_manager": str,
    "ldap_group_required": str,
    "ldap_group_admin": str,
    "ldap_group_expert": str,
    "ldap_timeout": int,
}
SECRET_FIELDS = {"ldap_bind_password"}


# ------------------------------------------------------------------ generic key/value
def get_setting(db: Session, key: str) -> str:
    row = db.get(AppSetting, key)
    return row.value if row is not None else SETTING_DEFAULTS.get(key, "")


def set_setting(db: Session, key: str, value: str) -> None:
    row = db.get(AppSetting, key)
    if row is None:
        db.add(AppSetting(key=key, value=value))
    else:
        row.value = value


def get_flag(db: Session, key: str) -> bool:
    return get_setting(db, key) == "1"


def features(db: Session) -> Dict[str, bool]:
    """Feature switches from Admin > Settings, exposed to templates as `features`."""
    return {
        "til": get_flag(db, SETTING_TIL_ENABLED),
        "signature": get_flag(db, SETTING_SIGNATURE_REQUIRED),
    }


def can_see_amounts(db: Session, viewer: User, target: User) -> bool:
    """Money amounts / rates of `target`'s declarations: hidden from employees (own sheets) when disabled.

    Admins always see them; a manager sees them on the sheets of the people below them
    (visibility of the target itself is enforced separately by access.py)."""
    if viewer.is_admin or viewer.id != target.id:
        return True
    return get_flag(db, SETTING_SHOW_AMOUNTS)


# ------------------------------------------------------------------ secrets
def _fernet(s: Settings) -> Fernet:
    key = hashlib.sha256(b"timelog-settings:" + s.secret_key.encode()).digest()
    return Fernet(base64.urlsafe_b64encode(key))


def encrypt_secret(s: Settings, value: str) -> str:
    return _fernet(s).encrypt(value.encode()).decode()


def decrypt_secret(s: Settings, token: str) -> Optional[str]:
    """None when the token cannot be decrypted (e.g. SECRET_KEY was rotated)."""
    try:
        return _fernet(s).decrypt(token.encode()).decode()
    except (InvalidToken, ValueError):
        return None


# ------------------------------------------------------------------ LDAP overrides
def _encode(attr: str, value) -> str:
    if LDAP_FIELDS[attr] is bool:
        return "1" if value else "0"
    return str(value)


def _decode(attr: str, raw: str):
    kind = LDAP_FIELDS[attr]
    if kind is bool:
        return raw == "1"
    if kind is int:
        return int(raw)
    return raw


def ldap_overrides(db: Session) -> Dict[str, str]:
    """Raw stored LDAP values, keyed by Settings attribute name."""
    rows = db.scalars(select(AppSetting).where(AppSetting.key.like(LDAP_PREFIX + "%")))
    out = {}
    for row in rows:
        attr = "ldap_" + row.key[len(LDAP_PREFIX):]
        if attr in LDAP_FIELDS:
            out[attr] = row.value
    return out


def effective_settings(db: Session, base: Settings) -> Settings:
    """Environment settings with the LDAP values stored in the database applied on top."""
    changes = {}
    for attr, raw in ldap_overrides(db).items():
        if attr in SECRET_FIELDS:
            value = decrypt_secret(base, raw)
            if value is None:
                continue  # unreadable secret: keep the environment value
            changes[attr] = value
        else:
            changes[attr] = _decode(attr, raw)
    return dataclasses.replace(base, **changes) if changes else base


def save_ldap_settings(db: Session, base: Settings, values: Dict[str, object]) -> None:
    """Persist LDAP values. A secret is only replaced when a new non-empty value is given."""
    for attr, value in values.items():
        if attr not in LDAP_FIELDS:
            raise KeyError(attr)
        key = LDAP_PREFIX + attr[len("ldap_"):]
        if attr in SECRET_FIELDS:
            if value:
                set_setting(db, key, encrypt_secret(base, str(value)))
            continue
        set_setting(db, key, _encode(attr, value))


def clear_ldap_settings(db: Session) -> int:
    rows = list(db.scalars(select(AppSetting).where(AppSetting.key.like(LDAP_PREFIX + "%"))))
    for row in rows:
        db.delete(row)
    return len(rows)


def secret_status(db: Session, base: Settings) -> str:
    """'database' | 'environment' | 'unreadable' | 'none' for the bind password."""
    raw = ldap_overrides(db).get("ldap_bind_password")
    if raw is not None:
        return "database" if decrypt_secret(base, raw) is not None else "unreadable"
    return "environment" if base.ldap_bind_password else "none"
