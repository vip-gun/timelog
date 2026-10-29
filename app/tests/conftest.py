import os
import re
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from timelog.config import Settings  # noqa: E402
from timelog.db import SessionLocal  # noqa: E402
from timelog.main import create_app  # noqa: E402
from timelog.models import User  # noqa: E402
from timelog.security import hash_password, throttle  # noqa: E402

PASSWORD = "Test-password1"


@pytest.fixture()
def settings(tmp_path):
    return Settings(
        secret_key="test-secret",
        database_url=f"sqlite:///{tmp_path / 'test.db'}",
        session_cookie_secure=False,
        bootstrap_admin_username="admin",
        bootstrap_admin_password=PASSWORD,
        overtime_lookback_months=1200,  # fixed test dates must stay inside the window
    )


@pytest.fixture()
def app(settings):
    throttle._failures.clear()
    if os.environ.get("TEST_DATABASE_URL"):
        # Run the suite against another database (e.g. PostgreSQL): start every test from an empty schema.
        from sqlalchemy import create_engine
        from timelog.db import Base
        import timelog.models  # noqa: F401  (register tables)
        settings.database_url = os.environ["TEST_DATABASE_URL"]
        engine = create_engine(settings.database_url)
        Base.metadata.drop_all(engine)
        engine.dispose()
    return create_app(settings)


@pytest.fixture()
def db(app):
    with SessionLocal() as session:
        yield session


@pytest.fixture()
def users(db):
    """head -> manager -> alice (normal), bob (expert), carol (normal); head -> dave."""
    spec = [
        ("head", None, False), ("manager", "head", False), ("alice", "manager", False),
        ("bob", "manager", True), ("carol", "manager", False), ("dave", "head", False),
    ]
    out = {}
    for name, mgr, expert in spec:
        u = User(username=name, auth_source="local", first_name=name.capitalize(), last_name="Test",
                 team="Production", is_expert=expert, password_hash=hash_password(PASSWORD),
                 email=f"{name}@example.test")
        db.add(u)
        db.flush()
        if mgr:
            u.manager_id = out[mgr].id
        out[name] = u
    db.commit()
    return out


def csrf_of(html: str) -> str:
    m = re.search(r'name="csrf_token" value="([^"]+)"', html)
    assert m, "csrf token not found"
    return m.group(1)


class Browser:
    def __init__(self, app):
        self.client = TestClient(app, base_url="http://testserver")
        self.csrf = ""

    def login(self, username, password=PASSWORD):
        page = self.client.get("/login")
        r = self.client.post("/login", data={"username": username, "password": password,
                                             "csrf_token": csrf_of(page.text), "next": "/"},
                             follow_redirects=False)
        if r.status_code == 303:
            self.csrf = csrf_of(self.client.get("/").text)
        return r

    def get(self, url, **kw):
        return self.client.get(url, **kw)

    def post(self, url, data=None, **kw):
        data = dict(data or {})
        data.setdefault("csrf_token", self.csrf)
        kw.setdefault("follow_redirects", False)
        return self.client.post(url, data=data, **kw)


@pytest.fixture()
def browser(app):
    def make(username=None):
        b = Browser(app)
        if username:
            assert b.login(username).status_code == 303
        return b
    return make
