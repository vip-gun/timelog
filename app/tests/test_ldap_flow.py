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
                         "mail": "alice@example.test", "department": "Prod", "manager": BOSS_DN})
BOSS = Entry(BOSS_DN, {"sAMAccountName": "boss", "givenName": "Big", "sn": "Boss"})
MEMBERS = {ALICE_DN: {"CN=Users", "CN=Experts"}}


class Conn:
    def __init__(self):
        self.entries = []
        self.result = {"result": 0, "description": "success", "message": ""}
        self.fail = None   # an ldap3 result dict to simulate a failed search

    def search(self, search_base, search_filter, search_scope=None, attributes=None, size_limit=0):
        base, flt = search_base, search_filter  # same parameter names as ldap3.Connection.search
        if self.fail:
            self.entries, self.result = [], self.fail
            return False
        if flt.startswith("(memberOf:"):
            self.entries = [Entry(base, {})] if flt.split(":=")[1].rstrip(")") in MEMBERS.get(base, ()) else []
        elif flt == "(objectClass=*)":
            self.entries = [BOSS] if base == BOSS_DN else []
        else:
            self.entries = [ALICE, BOSS] if "dup" in flt else ([ALICE] if "alice" in flt else [])
        return bool(self.entries)

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
    assert (p.username, p.first_name, p.last_name, p.email, p.team) == (
        "alice", "Alice", "Liddell", "alice@example.test", "Prod")
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


def _admin_browser(browser):
    b = browser()
    assert b.login("admin").status_code == 303
    return b


def test_lookup_users(ldap):
    got = ldap_auth.lookup_users(ldap, ["alice", "nobody", "dup"])
    assert got["alice"][0].first_name == "Alice" and got["alice"][0].manager.username == "boss" and got["alice"][1] == ""
    assert got["nobody"][0] is None and "not found" in got["nobody"][1]
    assert got["dup"][0] is None and "2 directory entries" in got["dup"][1]
    ldap.ldap_group_required = "CN=Others"
    assert "required LDAP group" in ldap_auth.lookup_users(ldap, ["alice"])["alice"][1]


def test_prepare_ldap_users_before_first_login(browser, db, users, ldap, monkeypatch):
    from timelog.models import AuditLog
    monkeypatch.setattr("timelog.routes.admin.effective_settings", lambda db_, base: ldap)
    admin = _admin_browser(browser)
    db.delete(users["alice"])                    # the fixture's local "alice" would be skipped as already existing
    db.commit()
    assert admin.get("/admin/users/ldap").status_code == 200
    r = admin.post("/admin/users/ldap", {"usernames": "alice\nnobody, manager", "lookup": "1", "team": "Data",
                                           "manager_id": str(users["dave"].id), "is_expert": "1"})
    assert r.status_code == 303
    db.expire_all()
    alice = db.query(User).filter_by(username="alice").one()
    assert (alice.auth_source, alice.first_name, alice.email, alice.last_login_at) == ("ldap", "Alice", "alice@example.test", None)
    assert (alice.team, alice.team_locked, alice.manager_id, alice.manager_locked, alice.is_expert) == ("Data", True, users["dave"].id, True, True)
    assert db.query(User).filter_by(username="nobody").count() == 0            # not in the directory
    assert db.query(User).filter_by(username="manager").one().auth_source == "local"   # existing user untouched
    assert db.query(User).filter_by(username="boss").count() == 0              # explicit manager: the directory one is not pulled in
    assert db.query(AuditLog).filter_by(action="user_preprovision").count() == 1
    assert "Not signed in yet" in admin.get("/admin/users").text

    # first sign-in: same record, completed from the directory, prepared team and manager kept
    p = authenticate(ldap, "alice", "good")
    again = ldap_auth.sync_user(db, p)
    db.commit()
    assert again.id == alice.id and again.ldap_dn == ALICE_DN and again.last_login_at is not None
    assert (again.team, again.manager_id) == ("Data", users["dave"].id)


def test_prepare_ldap_users_without_lookup_and_validation(browser, db, users):
    admin = _admin_browser(browser)
    admin.post("/admin/users/ldap", {"usernames": "newbie"})
    db.expire_all()
    u = db.query(User).filter_by(username="newbie").one()
    assert (u.auth_source, u.first_name, u.password_hash) == ("ldap", "", None)
    admin.post("/admin/users/ldap", {"usernames": "bad name!"})                  # invalid characters: nothing created
    db.expire_all()
    assert db.query(User).filter_by(username="bad").count() == 0
    assert browser("manager").post("/admin/users/ldap", {"usernames": "x"}).status_code in (302, 303, 403, 404)
    db.expire_all()
    assert db.query(User).filter_by(username="x").count() == 0


def test_diagnose_reports_a_failed_search_and_the_filter_used(ldap, monkeypatch):
    d = ldap_auth.diagnose(ldap, "nobody")
    assert not d.ok and "filter '(&(objectClass=user)(sAMAccountName=nobody))'" in d.steps[-1][2] and "Base 'DC=c'" in d.steps[-1][2]

    conn = Conn()
    conn.fail = {"result": 32, "description": "noSuchObject", "message": "0000208D: NameErr"}
    monkeypatch.setattr(ldap_auth, "_connect", lambda *a: conn)
    d = ldap_auth.diagnose(ldap, "alice")
    assert not d.ok and "noSuchObject" in d.steps[-1][2] and "base 'DC=c'" in d.steps[-1][2]
