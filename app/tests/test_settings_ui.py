import dataclasses

from timelog import __version__
from timelog.app_settings import decrypt_secret, effective_settings, encrypt_secret, ldap_overrides
from timelog.config import get_settings
from timelog.models import AppSetting

LDAP_FORM = {
    "ldap_enabled": "1", "ldap_url": "ldaps://127.0.0.1:1", "ldap_verify_cert": "1",
    "ldap_bind_dn": "CN=svc,DC=corp", "ldap_bind_password": "S3cret-bind!",
    "ldap_user_base_dn": "OU=Users,DC=corp", "ldap_user_filter": "(sAMAccountName={username})",
    "ldap_attr_username": "sAMAccountName", "ldap_attr_first_name": "givenName", "ldap_attr_last_name": "sn",
    "ldap_attr_display_name": "displayName", "ldap_attr_email": "mail",
    "ldap_attr_team": "department", "ldap_attr_manager": "manager", "ldap_timeout": "2",
}


def test_secret_roundtrip_and_key_rotation(settings):
    token = encrypt_secret(settings, "pw")
    assert token != "pw" and decrypt_secret(settings, token) == "pw"
    assert decrypt_secret(dataclasses.replace(settings, secret_key="other"), token) is None


def test_ldap_settings_saved_encrypted_and_override_env(browser, db, users):
    admin = browser("admin")
    assert browser("alice").get("/admin/settings").status_code == 404
    assert admin.post("/admin/settings/ldap", LDAP_FORM).status_code == 303
    db.expire_all()
    stored = db.get(AppSetting, "ldap.bind_password").value
    assert "S3cret" not in stored  # encrypted at rest
    eff = effective_settings(db, get_settings())
    assert eff.ldap_enabled and eff.ldap_url == "ldaps://127.0.0.1:1" and eff.ldap_bind_password == "S3cret-bind!"
    assert eff.ldap_timeout == 2
    page = admin.get("/admin/settings").text
    assert "S3cret" not in page and "stored encrypted" in page  # write-only
    assert "Windows login" in browser().get("/login").text  # login page reflects DB setting

    # empty password keeps the stored one
    admin.post("/admin/settings/ldap", {**LDAP_FORM, "ldap_bind_password": "", "ldap_timeout": "5"})
    db.expire_all()
    eff = effective_settings(db, get_settings())
    assert eff.ldap_bind_password == "S3cret-bind!" and eff.ldap_timeout == 5

    # reset falls back to the environment
    admin.post("/admin/settings/ldap/reset")
    db.expire_all()
    assert ldap_overrides(db) == {} and not effective_settings(db, get_settings()).ldap_enabled


def test_ldap_settings_validation(browser, db, users):
    admin = browser("admin")
    for bad in ({"ldap_url": "http://dc"}, {"ldap_user_filter": "(uid=x)"},
                {"ldap_start_tls": "1"}, {"ldap_ca_cert_data": "not a pem"}, {"ldap_timeout": "abc"}):
        admin.post("/admin/settings/ldap", {**LDAP_FORM, **bad})
        db.expire_all()
        assert ldap_overrides(db) == {}, bad


def test_ldap_test_connection_reports_failure(browser, db, users):
    admin = browser("admin")
    admin.post("/admin/settings/ldap", LDAP_FORM)
    r = admin.post("/admin/settings/ldap/test", {"test_username": "alice"})
    assert r.status_code == 200
    assert "Connect to directory" in r.text and 'class="ko"' in r.text


def test_footer_shows_version_and_build(browser, users):
    page = browser("alice").get("/").text
    assert f"v{__version__}" in page and ("dev build, started" in page or "built " in page)
    assert f"v{__version__}" in browser().get("/login").text
    assert browser().get("/healthz").text.startswith(f"ok {__version__}")
