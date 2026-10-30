from typing import Iterator, Optional

from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker


class Base(DeclarativeBase):
    pass


_engine: Optional[Engine] = None
SessionLocal = sessionmaker(autoflush=False, expire_on_commit=False)


def init_engine(url: str) -> Engine:
    global _engine
    kwargs = {"pool_pre_ping": True}
    if url.startswith("sqlite"):
        kwargs["connect_args"] = {"check_same_thread": False}
    _engine = create_engine(url, **kwargs)
    SessionLocal.configure(bind=_engine)
    return _engine


def get_engine() -> Engine:
    assert _engine is not None, "init_engine() not called"
    return _engine


def get_db() -> Iterator[Session]:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def ensure_columns(engine: Engine) -> list:
    """Additive schema upgrade: add the columns that models gained since the database was created.

    `create_all` creates missing tables but never alters existing ones. This adds missing columns of existing tables
    (nullable, or with a simple default) so an upgrade needs no manual migration. Anything that is not an added column
    (renames, type changes, dropped columns) still needs a real migration."""
    from sqlalchemy import inspect, text
    from sqlalchemy.schema import CreateColumn

    added = []
    insp = inspect(engine)
    for table in Base.metadata.sorted_tables:
        if not insp.has_table(table.name):
            continue
        have = {c["name"] for c in insp.get_columns(table.name)}
        for col in table.columns:
            if col.name in have:
                continue
            ddl = str(CreateColumn(col).compile(dialect=engine.dialect))
            default = ""
            if col.default is not None and getattr(col.default, "is_scalar", False):
                value = col.default.arg
                default = " DEFAULT " + ("TRUE" if value is True else "FALSE" if value is False else
                                         repr(value) if isinstance(value, (int, float)) else "'" + str(value).replace("'", "''") + "'")
            if not default:
                ddl = ddl.replace(" NOT NULL", "")   # no portable default (e.g. a Python-side one): add it nullable
            with engine.begin() as conn:
                conn.execute(text(f"ALTER TABLE {table.name} ADD COLUMN {ddl}{default}"))
            added.append(f"{table.name}.{col.name}")
    return added
