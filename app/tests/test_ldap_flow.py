"""LDAP authentication logic against a small in-memory directory (no server needed)."""
import dataclasses
from types import SimpleNamespace

import pytest

from timelog import ldap_auth
from timelog.config import Settings, get_settings
from timelog.ldap_auth import LdapUnavailable, authenticate, diagnose
from timelog.models import User

ALICE_DN, BOSS_DN = "CN=Alice,OU=U,DC=c", "CN=Boss,OU=U,DC=c"


class Entry:
    def __init__(self, dn, attrs):
        self.entry_dn, self._attrs = dn, attrs

    @property
    def entry_attributes(self):
        return list(self._attrs)

    def __getitem__(self, name):
        return SimpleNamespace(value=self._attrs[name])


ALICE = Entry(ALICE_DN, {"sAMAccountName": "alice", "givenName": "Alice", "sn": "Liddell", "displayName": "Alice L",
                         "mail": "alice@example.test", "employeeID": "E1", "department": "Prod", "manager": BOSS_DN})
BOSS = Entry(BOSS_DN, {"sAMAccountName": "boss", "givenName": "Big", "sn": "Boss"})
MEMBERS = {ALICE_DN: {"CN=Users", "CN=Experts"}}


class Conn:
    def __init__(self):
        self.entries = []

    def search(self, search_base, search_filter, search_scope=None, attributes=None, size_limit=0):
        base, flt = search_base, search_filter  # same parameter names as ldap3.Connection.search
        if flt.startswith("(memberOf:"):
            self.entries = [Entry(base, {})] if flt.split(":=")[1].rstrip(")") in MEMBERS.get(base, ()) else []
        elif flt == "(objectClass=*)":
            self.entries = [BOSS] if base == BOSS_DN else []
        else:
            self.entries = [ALICE, BOSS] if "dup" in flt else ([ALICE] if "alice" in flt else [])

    def unbind(self):
        pass


def fake_connect(s, server, user, password):
    if (user, password) in (("CN=svc", "p"), (ALICE_DN, "good")):
        return Conn()
    return None


@pytest.fixture()
def ldap(monkeypatch):
    monkeypatch.setattr(ldap_auth, "_connect", fake_connect)
    monkeypatch.setitem(MEMBERS, ALICE_DN, {"CN=Users", "CN=Experts"})
    return Settings(secret_key="x", ldap_enabled=True, ldap_url="ldaps://dc.example.test", ldap_bind_dn="CN=svc",
                    ldap_bind_password="p", ldap_user_base_dn="DC=c", ldap_group_required="CN=Users",
                    ldap_group_admin="CN=Admins", ldap_group_expert="CN=Experts")


def test_authenticate_success_maps_profile_groups_and_manager(ldap):
    p = authenticate(ldap, "alice", "good")
    assert (p.username, p.first_name, p.last_name, p.email, p.employee_id, p.team) == (
        "alice", "Alice", "Liddell", "alice@example.test", "E1", "Prod")
    assert p.is_expert is True and p.is_admin is False
    assert p.manager.username == "boss" and p.manager.dn == BOSS_DN


def test_authenticate_rejections(ldap, monkeypatch):
    assert authenticate(ldap, "alice", "") is None  # empty password = unauthenticated bind on AD
    assert authenticate(ldap, "alice", "bad") is None
    assert authenticate(ldap, "nobody", "good") is None
    assert authenticate(ldap, "dup", "good") is None  # ambiguous filter match
    monkeypatch.setitem(MEMBERS, ALICE_DN, {"CN=Experts"})  # not in the required group
    assert authenticate(ldap, "alice", "good") is None
    with pytest.raises(LdapUnavailable):
        authenticate(dataclasses.replace(ldap, ldap_bind_password="wrong"), "alice", "good")


def test_diagnose_reports_each_step(ldap):
    d = diagnose(ldap, "alice")
    assert d.ok and d.profile.username == "alice" and d.in_required_group is True
    assert [label for label, _, _ in d.steps] == [
        "Connect to directory", "Service account bind", "Find user", "Member of required group", "Manager"]
    assert diagnose(ldap).ok  # connection only
    assert not diagnose(ldap, "nobody").ok
    assert not diagnose(dataclasses.replace(ldap, ldap_bind_password="wrong")).ok
    assert not diagnose(dataclasses.replace(ldap, ldap_url="")).ok


def test_ldap_login_creates_user_and_unavailable_directory(browser, db, monkeypatch, ldap):
    get_settings().ldap_enabled = True
    monkeypatch.setattr("timelog.routes.auth.authenticate", lambda s, u, p: authenticate(ldap, u, p))
    assert browser().login("alice", "wrong").status_code == 401
    assert browser().login("alice", "good").status_code == 303
    db.expire_all()
    alice = db.query(User).filter_by(username="alice").one()
    assert alice.auth_source == "ldap" and alice.is_expert and alice.manager.username == "boss"

    def down(*_):
        raise LdapUnavailable("directory down")
    monkeypatch.setattr("timelog.routes.auth.authenticate", down)
    assert browser().login("bob", "x").status_code == 503
