"""Environment-based configuration. See .env.example for documentation of every variable."""
import os
from dataclasses import dataclass
from typing import Optional


def _bool(name: str, default: bool) -> bool:
    val = os.environ.get(name)
    if val is None or val == "":
        return default
    return val.strip().lower() in ("1", "true", "yes", "on")


def _str(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def _weekday(name: str, default: int) -> int:
    val = os.environ.get(name, "").strip().lower()[:3]
    days = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
    if not val:
        return default
    return days.index(val) if val in days else int(val)


def _int(name: str, default: int) -> int:
    val = os.environ.get(name)
    return int(val) if val else default


@dataclass
class Settings:
    app_name: str = "Timelog"
    base_url: str = ""
    secret_key: str = ""
    database_url: str = "sqlite:///./timelog.db"
    session_cookie_secure: bool = True
    session_max_age: int = 8 * 3600
    currency: str = "EUR"

    # Overtime night window, minutes since midnight
    night_start: int = 22 * 60
    night_end: int = 6 * 60
    # Overtime granularity (minutes) and how far back employees may declare overtime
    overtime_step_minutes: int = 30
    overtime_lookback_months: int = 12
    # Approvals up to this day of month M+1 go to payroll period M; later ones to the next period
    payroll_cutoff_day: int = 10
    # On-call rotation week start (0=Monday ... 4=Friday): weeks run Friday -> Thursday by default
    oncall_week_start: int = 4

    # Local accounts
    local_login_enabled: bool = True
    local_login_admin_only: bool = False
    bootstrap_admin_username: str = ""
    bootstrap_admin_password: str = ""

    # LDAP / Active Directory
    ldap_enabled: bool = False
    ldap_url: str = ""
    ldap_start_tls: bool = False
    ldap_ca_cert_file: str = ""
    ldap_ca_cert_data: str = ""  # PEM pasted in Admin > Settings (takes precedence over the file)
    ldap_verify_cert: bool = True
    ldap_bind_dn: str = ""
    ldap_bind_password: str = ""
    ldap_user_base_dn: str = ""
    ldap_user_filter: str = "(&(objectClass=user)(sAMAccountName={username}))"
    ldap_attr_username: str = "sAMAccountName"
    ldap_attr_first_name: str = "givenName"
    ldap_attr_last_name: str = "sn"
    ldap_attr_display_name: str = "displayName"
    ldap_attr_email: str = "mail"
    ldap_attr_employee_id: str = "employeeID"
    ldap_attr_team: str = "department"
    ldap_attr_manager: str = "manager"
    ldap_group_required: str = ""
    ldap_group_admin: str = ""
    ldap_group_expert: str = ""
    ldap_timeout: int = 10

    # SMTP notifications (optional)
    smtp_host: str = ""
    smtp_port: int = 25
    smtp_starttls: bool = False
    smtp_user: str = ""
    smtp_password: str = ""
    smtp_from: str = ""


    @staticmethod
    def _hhmm(value: str, default: int) -> int:
        if not value:
            return default
        h, m = value.split(":")
        return int(h) * 60 + int(m)

    @classmethod
    def from_env(cls) -> "Settings":
        s = cls(
            app_name=_str("APP_NAME", "Timelog"),
            base_url=_str("APP_BASE_URL").rstrip("/"),
            secret_key=_str("SECRET_KEY"),
            database_url=_str("DATABASE_URL", "sqlite:///./timelog.db"),
            session_cookie_secure=_bool("SESSION_COOKIE_SECURE", True),
            session_max_age=_int("SESSION_MAX_AGE", 8 * 3600),
            currency=_str("CURRENCY", "EUR"),
            night_start=cls._hhmm(_str("NIGHT_START"), 22 * 60),
            night_end=cls._hhmm(_str("NIGHT_END"), 6 * 60),
            overtime_step_minutes=_int("OVERTIME_STEP_MINUTES", 30),
            overtime_lookback_months=_int("OVERTIME_LOOKBACK_MONTHS", 12),
            payroll_cutoff_day=_int("PAYROLL_CUTOFF_DAY", 10),
            oncall_week_start=_weekday("ONCALL_WEEK_START", 4),
            local_login_enabled=_bool("LOCAL_LOGIN_ENABLED", True),
            local_login_admin_only=_bool("LOCAL_LOGIN_ADMIN_ONLY", False),
            bootstrap_admin_username=_str("BOOTSTRAP_ADMIN_USERNAME"),
            bootstrap_admin_password=os.environ.get("BOOTSTRAP_ADMIN_PASSWORD", ""),
            ldap_enabled=_bool("LDAP_ENABLED", False),
            ldap_url=_str("LDAP_URL"),
            ldap_start_tls=_bool("LDAP_START_TLS", False),
            ldap_ca_cert_file=_str("LDAP_CA_CERT_FILE"),
            ldap_verify_cert=_bool("LDAP_VERIFY_CERT", True),
            ldap_bind_dn=_str("LDAP_BIND_DN"),
            ldap_bind_password=os.environ.get("LDAP_BIND_PASSWORD", ""),
            ldap_user_base_dn=_str("LDAP_USER_BASE_DN"),
            ldap_user_filter=_str("LDAP_USER_FILTER", "(&(objectClass=user)(sAMAccountName={username}))"),
            ldap_attr_username=_str("LDAP_ATTR_USERNAME", "sAMAccountName"),
            ldap_attr_first_name=_str("LDAP_ATTR_FIRST_NAME", "givenName"),
            ldap_attr_last_name=_str("LDAP_ATTR_LAST_NAME", "sn"),
            ldap_attr_display_name=_str("LDAP_ATTR_DISPLAY_NAME", "displayName"),
            ldap_attr_email=_str("LDAP_ATTR_EMAIL", "mail"),
            ldap_attr_employee_id=_str("LDAP_ATTR_EMPLOYEE_ID", "employeeID"),
            ldap_attr_team=_str("LDAP_ATTR_TEAM", "department"),
            ldap_attr_manager=_str("LDAP_ATTR_MANAGER", "manager"),
            ldap_group_required=_str("LDAP_GROUP_REQUIRED"),
            ldap_group_admin=_str("LDAP_GROUP_ADMIN"),
            ldap_group_expert=_str("LDAP_GROUP_EXPERT"),
            ldap_timeout=_int("LDAP_TIMEOUT", 10),
            smtp_host=_str("SMTP_HOST"),
            smtp_port=_int("SMTP_PORT", 25),
            smtp_starttls=_bool("SMTP_STARTTLS", False),
            smtp_user=_str("SMTP_USER"),
            smtp_password=os.environ.get("SMTP_PASSWORD", ""),
            smtp_from=_str("SMTP_FROM"),
        )
        if not s.secret_key or s.secret_key.startswith("change-me"):
            raise RuntimeError("SECRET_KEY must be set to a random value (e.g. `openssl rand -hex 32`).")
        if s.bootstrap_admin_password.startswith("change-me"):
            raise RuntimeError("BOOTSTRAP_ADMIN_PASSWORD still has the .env.example placeholder - choose a password.")
        return s


_settings: Optional[Settings] = None


def get_settings() -> Settings:
    global _settings
    if _settings is None:
        _settings = Settings.from_env()
    return _settings


def set_settings(s: Settings) -> None:
    """Used by tests to inject settings."""
    global _settings
    _settings = s
