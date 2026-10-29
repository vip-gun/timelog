"""Create demo local users for testing/debugging:  python -m timelog.demo [password]

Without a password argument a random one is generated and printed once (no password in the code).

Hierarchy:  head (dept head) -> manager (line manager) -> alice (normal), bob (expert)
            manager -> carol (normal);  head -> dave (normal, other team)
"""
import secrets
import sys

from sqlalchemy import select

from .config import get_settings
from .db import Base, SessionLocal, init_engine
from .models import User
from .security import hash_password
from .seed import seed

DEMO_USERS = [
    # username, first, last, team, manager, expert
    ("head", "Helen", "Head", "IT Management", None, False),
    ("manager", "Mark", "Manager", "Production", "head", False),
    ("alice", "Alice", "Normal", "Production", "manager", False),
    ("bob", "Bob", "Expert", "Production", "manager", True),
    ("carol", "Carol", "Normal", "Production", "manager", False),
    ("dave", "Dave", "Other", "Service Desk", "head", False),
]


def main() -> None:
    password = sys.argv[1] if len(sys.argv) > 1 else secrets.token_urlsafe(12)
    settings = get_settings()
    Base.metadata.create_all(init_engine(settings.database_url))
    with SessionLocal() as db:
        seed(db, settings)
        by_name = {}
        for username, first, last, team, manager, expert in DEMO_USERS:
            user = db.scalar(select(User).where(User.username == username))
            if user is None:
                user = User(username=username, auth_source="local")
                db.add(user)
            user.first_name, user.last_name, user.team, user.is_expert = first, last, team, expert
            user.password_hash = hash_password(password)
            by_name[username] = user
            db.flush()
        for username, *_rest, manager, _expert in DEMO_USERS:
            by_name[username].manager_id = by_name[manager].id if manager else None
        db.commit()
    print(f"Demo users created: {', '.join(u[0] for u in DEMO_USERS)} (password: {password})")


if __name__ == "__main__":
    main()
