"""LDAP / Active Directory authentication (search-then-bind) and user synchronisation."""
import logging
import ssl
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

from sqlalchemy import select
from sqlalchemy.orm import Session

from .config import Settings
from .models import User, utcnow

log = logging.getLogger(__name__)

# AD "LDAP_MATCHING_RULE_IN_CHAIN": resolves nested group membership server-side.
_IN_CHAIN = "1.2.840.113556.1.4.1941"


class LdapUnavailable(Exception):
    """Directory unreachable or misconfigured (distinct from bad credentials)."""


@dataclass
class LdapProfile:
    dn: str
    username: str
    first_name: str = ""
    last_name: str = ""
    display_name: str = ""
    email: str = ""
    employee_id: str = ""
    team: str = ""
    manager: Optional["LdapProfile"] = None
    is_admin: Optional[bool] = None  # None = not managed by LDAP
    is_expert: Optional[bool] = None
    is_procurement: Optional[bool] = None


def _server(s: Settings):
    from ldap3 import NONE, Server, Tls

    tls = Tls(
        validate=ssl.CERT_REQUIRED if s.ldap_verify_cert else ssl.CERT_NONE,
        ca_certs_file=None if s.ldap_ca_cert_data.strip() else (s.ldap_ca_cert_file or None),
        ca_certs_data=s.ldap_ca_cert_data.strip() or None,
    )
    return Server(
        s.ldap_url,
        use_ssl=s.ldap_url.lower().startswith("ldaps://"),
        tls=tls,
        get_info=NONE,
        connect_timeout=s.ldap_timeout,
    )


def _connect(s: Settings, server, user: str, password: str):
    """Returns a bound connection, or None if credentials are rejected."""
    from ldap3 import Connection
    from ldap3.core.exceptions import LDAPBindError, LDAPException

    # auto_referrals=False: a search from the domain root makes AD answer with referrals (DomainDnsZones, other domains);
    # ldap3 would follow them to hosts that may not resolve ("LDAPSocketOpenError: invalid server address").
    conn = Connection(server, user=user, password=password, receive_timeout=s.ldap_timeout, raise_exceptions=False,
                      auto_referrals=False)
    try:
        conn.open()
        if s.ldap_start_tls:
            conn.start_tls()
        if not conn.bind():
            conn.unbind()
            return None
    except LDAPBindError:
        return None
    except LDAPException as exc:
        raise LdapUnavailable(str(exc)) from exc
    return conn


def _attr(entry, name: str) -> str:
    if not name or name not in entry.entry_attributes:
        return ""
    val = entry[name].value
    if isinstance(val, list):
        val = val[0] if val else ""
    return str(val or "").strip()


def _attributes(s: Settings) -> list:
    names = [
        s.ldap_attr_username, s.ldap_attr_first_name, s.ldap_attr_last_name,
        s.ldap_attr_display_name, s.ldap_attr_email, s.ldap_attr_employee_id,
        s.ldap_attr_team, s.ldap_attr_manager,
    ]
    return [n for n in names if n]


def _profile_from_entry(s: Settings, entry) -> LdapProfile:
    return LdapProfile(
        dn=entry.entry_dn,
        username=_attr(entry, s.ldap_attr_username).lower(),
        first_name=_attr(entry, s.ldap_attr_first_name),
        last_name=_attr(entry, s.ldap_attr_last_name),
        display_name=_attr(entry, s.ldap_attr_display_name),
        email=_attr(entry, s.ldap_attr_email),
        employee_id=_attr(entry, s.ldap_attr_employee_id),
        team=_attr(entry, s.ldap_attr_team),
    )


def _is_member(s: Settings, conn, user_dn: str, group_dn: str) -> bool:
    from ldap3 import BASE
    from ldap3.utils.conv import escape_filter_chars

    flt = f"(memberOf:{_IN_CHAIN}:={escape_filter_chars(group_dn)})"
    conn.search(search_base=user_dn, search_filter=flt, search_scope=BASE, attributes=[])
    return len(conn.entries) > 0


def _find_user(s: Settings, svc, username: str):
    from ldap3 import SUBTREE
    from ldap3.utils.conv import escape_filter_chars

    flt = s.ldap_user_filter.replace("{username}", escape_filter_chars(username))
    ok = svc.search(s.ldap_user_base_dn, flt, search_scope=SUBTREE, attributes=_attributes(s), size_limit=2)
    result = svc.result or {}
    # size limit exceeded (4) is expected with size_limit=2; any other failure must not look like "no such user"
    if not ok and result.get("result") not in (0, 4):
        raise LdapUnavailable(f"search failed: {result.get('description')} - {result.get('message') or 'no detail'} "
                              f"(base '{s.ldap_user_base_dn}', filter '{flt}')")
    return list(svc.entries)


def _enrich(s: Settings, svc, entry, profile: LdapProfile) -> None:
    """Groups and manager of a found user."""
    from ldap3 import BASE

    if s.ldap_group_admin:
        profile.is_admin = _is_member(s, svc, profile.dn, s.ldap_group_admin)
    if s.ldap_group_expert:
        profile.is_expert = _is_member(s, svc, profile.dn, s.ldap_group_expert)
    if s.ldap_group_procurement:
        profile.is_procurement = _is_member(s, svc, profile.dn, s.ldap_group_procurement)
    manager_dn = _attr(entry, s.ldap_attr_manager)
    if manager_dn:
        svc.search(manager_dn, "(objectClass=*)", search_scope=BASE, attributes=_attributes(s))
        if len(svc.entries) == 1:
            profile.manager = _profile_from_entry(s, svc.entries[0])


@dataclass
class Diagnosis:
    ok: bool
    steps: list  # [(label, ok, detail)]
    profile: Optional[LdapProfile] = None
    in_required_group: Optional[bool] = None


def diagnose(s: Settings, username: str = "") -> Diagnosis:
    """Admin "test connection": service bind, then (optionally) look a user up - no user password involved."""
    steps = []
    try:
        if not s.ldap_url:
            raise LdapUnavailable("LDAP URL is not configured.")
        server = _server(s)
        svc = _connect(s, server, s.ldap_bind_dn, s.ldap_bind_password)
    except Exception as exc:  # includes TLS/CA errors from a malformed certificate
        steps.append(("Connect to directory", False, str(exc) or type(exc).__name__))
        return Diagnosis(False, steps)
    if svc is None:
        steps.append(("Connect to directory", True, s.ldap_url))
        steps.append(("Service account bind", False, "Invalid credentials for the bind DN."))
        return Diagnosis(False, steps)
    steps.append(("Connect to directory", True, s.ldap_url + (" (StartTLS)" if s.ldap_start_tls else "")))
    steps.append(("Service account bind", True, s.ldap_bind_dn or "(anonymous)"))
    try:
        if not username:
            return Diagnosis(True, steps)
        entries = _find_user(s, svc, username)
        if len(entries) != 1:
            flt = s.ldap_user_filter.replace("{username}", username)
            steps.append(("Find user", False, f"{len(entries)} entries match the user filter (expected exactly 1). "
                          f"Base '{s.ldap_user_base_dn}', filter '{flt}'."))
            return Diagnosis(False, steps)
        profile = _profile_from_entry(s, entries[0])
        steps.append(("Find user", True, profile.dn))
        required = _is_member(s, svc, profile.dn, s.ldap_group_required) if s.ldap_group_required else None
        if required is not None:
            steps.append(("Member of required group", required, s.ldap_group_required))
        _enrich(s, svc, entries[0], profile)
        steps.append(("Manager", profile.manager is not None,
                      profile.manager.dn if profile.manager else f"attribute '{s.ldap_attr_manager}' empty or not resolvable"))
        return Diagnosis(required is not False, steps, profile, required)
    except Exception as exc:  # report any directory error to the admin instead of a 500
        steps.append(("Search", False, f"{type(exc).__name__}: {exc}"))
        return Diagnosis(False, steps)
    finally:
        svc.unbind()


def lookup_users(s: Settings, usernames: List[str]) -> Dict[str, Tuple[Optional[LdapProfile], str]]:
    """Look users up in the directory without their password (service account only), to prepare accounts in advance.

    {username: (profile, "")} when found, {username: (None, reason)} otherwise. A user outside the required group is
    returned with a warning text, since they could not sign in until added to it. Raises LdapUnavailable."""
    if not s.ldap_url:
        raise LdapUnavailable("LDAP URL is not configured.")
    svc = _connect(s, _server(s), s.ldap_bind_dn, s.ldap_bind_password)
    if svc is None:
        raise LdapUnavailable("Service account bind failed - check the bind DN and password.")
    out: Dict[str, Tuple[Optional[LdapProfile], str]] = {}
    try:
        for username in usernames:
            entries = _find_user(s, svc, username)
            if len(entries) != 1:
                out[username] = (None, "not found in the directory" if not entries else f"{len(entries)} directory entries match")
                continue
            profile = _profile_from_entry(s, entries[0])
            profile.username = profile.username or username.lower()
            warning = ""
            if s.ldap_group_required and not _is_member(s, svc, profile.dn, s.ldap_group_required):
                warning = "not in the required LDAP group: cannot sign in until added to it"
            _enrich(s, svc, entries[0], profile)
            out[username] = (profile, warning)
    finally:
        svc.unbind()
    return out


def authenticate(s: Settings, username: str, password: str) -> Optional[LdapProfile]:
    """Verify credentials against the directory. Returns None on bad credentials / not allowed."""
    # An empty password would perform an "unauthenticated bind" that succeeds on AD.
    if not username or not password:
        return None

    server = _server(s)
    svc = _connect(s, server, s.ldap_bind_dn, s.ldap_bind_password)
    if svc is None:
        raise LdapUnavailable("Service account bind failed - check LDAP_BIND_DN / LDAP_BIND_PASSWORD.")
    try:
        entries = _find_user(s, svc, username)
        if len(entries) != 1:
            return None
        entry = entries[0]
        profile = _profile_from_entry(s, entry)
        profile.username = profile.username or username.lower()

        user_conn = _connect(s, server, profile.dn, password)
        if user_conn is None:
            return None
        user_conn.unbind()

        if s.ldap_group_required and not _is_member(s, svc, profile.dn, s.ldap_group_required):
            log.info("LDAP user %s not in required group", profile.username)
            return None
        _enrich(s, svc, entry, profile)
        return profile
    finally:
        svc.unbind()


def _upsert(db: Session, p: LdapProfile) -> User:
    user = db.scalar(select(User).where(User.ldap_dn == p.dn))
    if user is None and p.username:
        user = db.scalar(select(User).where(User.username == p.username))
    if user is None:
        user = User(username=p.username, auth_source="ldap")
        db.add(user)
    if user.auth_source == "ldap":
        user.ldap_dn = p.dn
    for attr in ("first_name", "last_name", "display_name", "email", "employee_id", "team"):
        val = getattr(p, attr)
        if val and not (attr == "team" and user.team_locked):
            setattr(user, attr, val)
    return user


def sync_user(db: Session, p: LdapProfile, login: bool = True) -> User:
    """Create/update the local record of an LDAP user (and their manager): after a successful login, or (login=False)
    when an administrator prepares the account in advance."""
    user = _upsert(db, p)
    if p.is_admin is not None:
        user.is_admin = p.is_admin
    if p.is_expert is not None:
        user.is_expert = p.is_expert
    if p.is_procurement is not None:
        user.is_procurement = p.is_procurement
    if p.manager is not None and not user.manager_locked:
        manager = _upsert(db, p.manager)
        db.flush()
        if manager.id != user.id:
            user.manager_id = manager.id
    if login:
        user.last_login_at = utcnow()
    db.flush()
    return user
