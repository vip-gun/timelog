"""Database backups made by the application itself (Admin > Backup).

A backup is a gzip-compressed JSON file with every table of the schema (database independent: works the same on
PostgreSQL and SQLite), **encrypted with AES-256-GCM** (key = PBKDF2-HMAC-SHA256 of the backup passphrase with a
random salt per file; header authenticated). The passphrase is `BACKUP_PASSPHRASE`, or derived from `SECRET_KEY` when
it is not set. Restoring replaces the whole content of the database. Backups can run on a schedule (daily / weekly,
UTC) configured in the admin interface.
"""
import base64
import datetime as dt
import gzip
import hashlib
import json
import logging
import os
import re
import threading
import time
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Callable, Dict, List, Optional

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from sqlalchemy import Table, delete, func, insert, select, text, update
from sqlalchemy.engine import Connection, Engine

from . import __version__
from . import app_settings
from .db import Base
from .models import utcnow

log = logging.getLogger(__name__)

FORMAT = "timelog-backup"
FORMAT_VERSION = 1
NAME_RE = re.compile(r"^timelog-\d{8}-\d{6}(-[a-z]+)?\.tlbk$")
LEGACY_RE = re.compile(r"^timelog-\d{8}-\d{6}(-[a-z]+)?\.json\.gz$")  # v1.2.0 unencrypted files, encrypted at startup
MAGIC = b"TLBK"
ENC_VERSION = 1
PBKDF2_ROUNDS = 600_000
LABELS = ("manual", "auto", "pre-restore", "uploaded")
MAX_UPLOAD_BYTES = 200 * 1024 * 1024
_lock = threading.Lock()


class BackupError(ValueError):
    pass


@dataclass
class BackupFile:
    name: str
    size: int
    created: dt.datetime
    kind: str


# ------------------------------------------------------------------ value encoding
def _encode(value):
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, bytes):
        return base64.b64encode(value).decode()
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, (dt.datetime, dt.date)):
        return value.isoformat()
    raise BackupError(f"Unsupported value type {type(value).__name__}")


def _decode(column, value):
    if value is None:
        return None
    kind = column.type.python_type
    if kind is bytes:
        return base64.b64decode(value)
    if kind is Decimal:
        return Decimal(value)
    if kind is dt.datetime:
        return dt.datetime.fromisoformat(value)
    if kind is dt.date:
        return dt.date.fromisoformat(value)
    return value


def _tables() -> List[Table]:
    return list(Base.metadata.sorted_tables)


# ------------------------------------------------------------------ encryption
def _derive(passphrase: str, salt: bytes) -> bytes:
    return hashlib.pbkdf2_hmac("sha256", passphrase.encode(), salt, PBKDF2_ROUNDS, 32)


def encrypt(data: bytes, passphrase: str) -> bytes:
    """MAGIC | version | salt(16) | nonce(12) | AES-256-GCM ciphertext+tag (header is authenticated)."""
    salt, nonce = os.urandom(16), os.urandom(12)
    header = MAGIC + bytes([ENC_VERSION]) + salt + nonce
    return header + AESGCM(_derive(passphrase, salt)).encrypt(nonce, data, header)


def is_encrypted(blob: bytes) -> bool:
    return blob[:4] == MAGIC


def decrypt(blob: bytes, passphrase: str) -> bytes:
    if not is_encrypted(blob) or len(blob) < 33 + 16:
        raise BackupError("This file is not an encrypted Timelog backup.")
    if blob[4] != ENC_VERSION:
        raise BackupError(f"Unsupported backup encryption version {blob[4]}.")
    header, body = blob[:33], blob[33:]
    try:
        return AESGCM(_derive(passphrase, header[5:21])).decrypt(header[21:33], body, header)
    except InvalidTag:
        raise BackupError("The backup cannot be decrypted: wrong passphrase / SECRET_KEY, or the file is damaged.")


def passphrase_of(settings) -> str:
    """BACKUP_PASSPHRASE when set, else a key derived from SECRET_KEY."""
    return settings.backup_passphrase or "timelog-backup:" + settings.secret_key


# ------------------------------------------------------------------ files
def _path(directory: str, name: str) -> Path:
    if not NAME_RE.match(name):
        raise BackupError("Invalid backup name.")
    return Path(directory) / name


def _kind(name: str) -> str:
    m = NAME_RE.match(name)
    label = (m.group(1) or "")[1:] if m else ""
    return "pre-restore" if label == "pre" else (label or "manual")


def list_backups(directory: str) -> List[BackupFile]:
    root = Path(directory)
    if not root.is_dir():
        return []
    out = []
    for p in root.iterdir():
        if NAME_RE.match(p.name) and p.is_file():
            st = p.stat()
            out.append(BackupFile(p.name, st.st_size, dt.datetime.fromtimestamp(st.st_mtime, dt.timezone.utc).replace(tzinfo=None), _kind(p.name)))
    return sorted(out, key=lambda b: b.name, reverse=True)


def backup_path(directory: str, name: str) -> Path:
    p = _path(directory, name)
    if not p.is_file():
        raise BackupError("Backup not found.")
    return p


def delete_backup(directory: str, name: str) -> None:
    backup_path(directory, name).unlink()


# ------------------------------------------------------------------ create
def _snapshot(engine: Engine) -> dict:
    conn = engine.connect()
    try:
        if engine.dialect.name == "postgresql":
            conn = conn.execution_options(isolation_level="REPEATABLE READ")  # one consistent snapshot
        tables = {}
        with conn.begin():
            for table in _tables():
                cols = [c.name for c in table.columns]
                rows = [[_encode(v) for v in row] for row in conn.execute(select(table).order_by(*table.primary_key.columns))]
                tables[table.name] = {"columns": cols, "rows": rows}
        return tables
    finally:
        conn.close()


def _free_name(directory: str, when: dt.datetime, suffix: str) -> str:
    """timelog-<UTC stamp><suffix>.tlbk; the stamp moves on by a second while the name is taken."""
    while True:
        name = f"timelog-{when:%Y%m%d-%H%M%S}{suffix}.tlbk"
        if not (Path(directory) / name).exists():
            return name
        when += dt.timedelta(seconds=1)


def create_backup(engine: Engine, directory: str, passphrase: str, label: str = "manual") -> BackupFile:
    suffix = {"manual": "", "auto": "-auto", "pre-restore": "-pre", "uploaded": "-uploaded"}[label]
    with _lock:
        Path(directory).mkdir(parents=True, exist_ok=True)
        now = utcnow()
        name = _free_name(directory, now, suffix)
        payload = {"format": FORMAT, "version": FORMAT_VERSION, "app_version": __version__,
                   "created": now.isoformat() + "Z", "tables": _snapshot(engine)}
        data = gzip.compress(json.dumps(payload, separators=(",", ":")).encode())
        target = Path(directory) / name
        tmp = target.with_suffix(".tmp")
        tmp.write_bytes(encrypt(data, passphrase))
        tmp.chmod(0o600)
        tmp.replace(target)
    st = target.stat()
    return BackupFile(name, st.st_size, now, label)


def store_uploaded(directory: str, blob: bytes, source_passphrase: str, passphrase: str) -> str:
    """Validate an uploaded backup (decrypting it with its own passphrase) and store it encrypted with ours."""
    payload = load_payload(blob, source_passphrase)
    data = gzip.compress(json.dumps(payload, separators=(",", ":")).encode())
    with _lock:
        Path(directory).mkdir(parents=True, exist_ok=True)
        name = _free_name(directory, utcnow(), "-uploaded")
        (Path(directory) / name).write_bytes(encrypt(data, passphrase))
        (Path(directory) / name).chmod(0o600)
    return name


def encrypt_legacy(directory: str, passphrase: str) -> int:
    """Encrypt unencrypted .json.gz backups left by v1.2.0 (and remove the plain files)."""
    root = Path(directory)
    done = 0
    if root.is_dir():
        for p in root.iterdir():
            if LEGACY_RE.match(p.name) and p.is_file():
                target = p.with_name(p.name[:-len(".json.gz")] + ".tlbk")
                if not target.exists():
                    target.write_bytes(encrypt(p.read_bytes(), passphrase))
                    target.chmod(0o600)
                p.unlink()
                done += 1
    return done


def prune_auto(directory: str, keep: int) -> int:
    """Keep the newest `keep` automatic backups; manual, uploaded and pre-restore ones are never removed."""
    autos = [b for b in list_backups(directory) if b.kind == "auto"]
    removed = 0
    for old in autos[max(keep, 1):]:
        delete_backup(directory, old.name)
        removed += 1
    return removed


# ------------------------------------------------------------------ read / validate
def load_payload(source, passphrase: str) -> dict:
    """Decrypt, parse and validate a backup (bytes or path). Raises BackupError."""
    blob = source if isinstance(source, bytes) else Path(source).read_bytes()
    try:
        payload = json.loads(gzip.decompress(decrypt(blob, passphrase)).decode("utf-8"))
    except (OSError, EOFError, ValueError) as exc:
        raise BackupError("This file is not a valid Timelog backup (cannot read it).") from exc
    if not isinstance(payload, dict) or payload.get("format") != FORMAT:
        raise BackupError("This file is not a Timelog backup.")
    if payload.get("version") != FORMAT_VERSION:
        raise BackupError(f"Unsupported backup format version {payload.get('version')!r}.")
    known = {t.name: t for t in _tables()}
    tables = payload.get("tables")
    if not isinstance(tables, dict):
        raise BackupError("Backup content is missing.")
    for name, data in tables.items():
        if name not in known:
            raise BackupError(f"Unknown table {name!r}: the backup comes from a different version of the application.")
        cols = data.get("columns") if isinstance(data, dict) else None
        if not isinstance(cols, list) or not isinstance(data.get("rows"), list):
            raise BackupError(f"Table {name!r} is malformed.")
        unknown = set(cols) - {c.name for c in known[name].columns}
        if unknown:
            raise BackupError(f"Table {name!r} has unknown columns {sorted(unknown)}: schema mismatch.")
        if any(not isinstance(r, list) or len(r) != len(cols) for r in data["rows"]):
            raise BackupError(f"Table {name!r} has malformed rows.")
    return payload


def describe(payload: dict) -> Dict[str, object]:
    return {"app_version": payload.get("app_version", "?"), "created": payload.get("created", "?"),
            "rows": {n: len(d["rows"]) for n, d in payload["tables"].items()}}


# ------------------------------------------------------------------ restore
def _self_refs(table: Table) -> List[str]:
    return [c.name for c in table.columns for fk in c.foreign_keys if fk.column.table is table]


def _insert_table(conn: Connection, table: Table, data: dict) -> None:
    cols = {c.name: c for c in table.columns}
    refs = set(_self_refs(table))
    pk = [c.name for c in table.primary_key.columns]
    rows = []
    deferred = []
    for raw in data["rows"]:
        row = {name: _decode(cols[name], v) for name, v in zip(data["columns"], raw)}
        if refs:  # rows pointing at other rows of the same table are linked after all rows exist
            link = {r: row[r] for r in refs if row.get(r) is not None}
            if link:
                deferred.append(({k: row[k] for k in pk}, link))
            for r in refs:
                row[r] = None  # same keys in every row (batch insert)
        rows.append(row)
    for i in range(0, len(rows), 500):
        conn.execute(insert(table), rows[i:i + 500])
    for key, link in deferred:
        conn.execute(update(table).where(*[table.c[k] == v for k, v in key.items()]).values(**link))


def _reset_sequences(conn: Connection) -> None:
    if conn.dialect.name != "postgresql":
        return
    for table in _tables():
        for col in table.primary_key.columns:
            if col.autoincrement is not False and col.type.python_type is int and len(table.primary_key.columns) == 1:
                conn.execute(text(
                    f"SELECT setval(pg_get_serial_sequence('{table.name}', '{col.name}'), "
                    f"COALESCE((SELECT MAX({col.name}) FROM {table.name}), 1), "
                    f"(SELECT MAX({col.name}) FROM {table.name}) IS NOT NULL)"))


def restore_backup(engine: Engine, directory: str, name: str, passphrase: str) -> dict:
    """Replace the content of the database with the backup. A safety backup is written first."""
    payload = load_payload(backup_path(directory, name), passphrase)
    create_backup(engine, directory, passphrase, "pre-restore")
    with _lock:
        with engine.begin() as conn:
            for table in reversed(_tables()):
                conn.execute(delete(table))
            for table in _tables():
                data = payload["tables"].get(table.name)
                if data and data["rows"]:
                    _insert_table(conn, table, data)
            _reset_sequences(conn)
    return describe(payload)


# ------------------------------------------------------------------ schedule
WEEKDAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]


@dataclass
class Schedule:
    enabled: bool = True
    frequency: str = "daily"      # daily | weekly
    weekday: int = 0              # 0 = Monday (weekly only)
    minute: int = 2 * 60          # minutes after midnight, UTC
    keep: int = 14                # automatic backups kept
    since: Optional[dt.datetime] = None  # the schedule was saved at this time: no catch-up run before it


def load_schedule(db) -> Schedule:
    get = lambda k, d: app_settings.get_setting(db, "backup." + k) or d  # noqa: E731
    try:
        h, m = get("time", "02:00").split(":")
        since = get("since", "")
        return Schedule(enabled=get("enabled", "1") == "1", frequency=get("frequency", "daily"),
                        weekday=int(get("weekday", "0")), minute=int(h) * 60 + int(m), keep=int(get("keep", "14")),
                        since=dt.datetime.fromisoformat(since) if since else None)
    except ValueError:
        return Schedule()


def save_schedule(db, sch: Schedule) -> None:
    for key, value in (("enabled", "1" if sch.enabled else "0"), ("frequency", sch.frequency),
                       ("weekday", str(sch.weekday)), ("time", f"{sch.minute // 60:02d}:{sch.minute % 60:02d}"),
                       ("keep", str(sch.keep)), ("since", utcnow().isoformat())):
        app_settings.set_setting(db, "backup." + key, value)


def occurrence_before(now: dt.datetime, sch: Schedule) -> dt.datetime:
    """The most recent scheduled run time at or before `now` (UTC)."""
    at = now.replace(hour=sch.minute // 60, minute=sch.minute % 60, second=0, microsecond=0)
    if sch.frequency == "weekly":
        at -= dt.timedelta(days=(at.weekday() - sch.weekday) % 7)
    if at > now:
        at -= dt.timedelta(days=7 if sch.frequency == "weekly" else 1)
    return at


def next_occurrence(now: dt.datetime, sch: Schedule) -> dt.datetime:
    step = dt.timedelta(days=7 if sch.frequency == "weekly" else 1)
    return occurrence_before(now, sch) + step


def is_due(now: dt.datetime, sch: Schedule, last_auto: Optional[dt.datetime]) -> bool:
    if not sch.enabled:
        return False
    reference = max([t for t in (last_auto, sch.since) if t is not None], default=None)
    due_at = occurrence_before(now, sch)
    return reference is None or due_at > reference


def start_scheduler(engine: Engine, directory: str, get_schedule: Callable[[], Schedule], passphrase: str,
                    check_seconds: int = 60) -> threading.Thread:
    """Daemon thread: runs the configured schedule (re-read every tick, so UI changes apply at once)."""

    def loop() -> None:
        while True:
            try:
                sch = get_schedule()
                autos = [b for b in list_backups(directory) if b.kind == "auto"]
                if is_due(utcnow(), sch, autos[0].created if autos else None):
                    created = create_backup(engine, directory, passphrase, "auto")
                    removed = prune_auto(directory, sch.keep)
                    log.info("Scheduled backup %s written (%d old removed)", created.name, removed)
            except Exception:  # keep the scheduler alive whatever happens
                log.exception("Scheduled backup failed")
            time.sleep(check_seconds)

    thread = threading.Thread(target=loop, name="backup-scheduler", daemon=True)
    thread.start()
    return thread
