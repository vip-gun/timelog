"""Admin > Backup: encrypted create/download/upload/delete/restore and the schedule (SQLite and PostgreSQL)."""
import datetime as dt
import gzip
import json
from pathlib import Path

import pytest

from timelog import backup
from timelog.db import SessionLocal, get_engine
from timelog.models import AuditLog, OvertimeEntry, Sheet, Signature, User

from test_v1_features import SIG, _ot


def _dir(settings, tmp_path):
    settings.backup_dir = str(tmp_path / "bk")
    return settings.backup_dir


def test_backup_page_is_admin_only(browser, users, settings, tmp_path):
    _dir(settings, tmp_path)
    assert browser("admin").get("/admin/backup").status_code == 200
    assert browser("alice").get("/admin/backup").status_code in (403, 404)
    assert browser("alice").post("/admin/backup/create").status_code in (403, 404)


def test_create_download_delete(browser, db, users, settings, tmp_path):
    d = _dir(settings, tmp_path)
    admin = browser("admin")
    admin.post("/admin/backup/create")
    files = backup.list_backups(d)
    assert len(files) == 1 and files[0].kind == "manual"
    page = admin.get("/admin/backup").text
    assert files[0].name in page
    got = admin.get(f"/admin/backup/download/{files[0].name}")
    assert got.status_code == 200 and backup.is_encrypted(got.content)
    assert b"alice" not in got.content and b"PBKDF2" not in got.content  # nothing readable in the file
    payload = backup.load_payload(got.content, backup.passphrase_of(settings))
    assert "users" in payload["tables"]
    with pytest.raises(backup.BackupError):
        backup.load_payload(got.content, "another passphrase")
    tampered = bytearray(got.content)
    tampered[-5] ^= 1
    with pytest.raises(backup.BackupError):
        backup.load_payload(bytes(tampered), backup.passphrase_of(settings))
    assert admin.get("/admin/backup/download/../../etc/passwd").status_code in (404, 405)
    assert admin.get("/admin/backup/download/evil.tlbk").status_code == 404
    admin.post(f"/admin/backup/delete/{files[0].name}")
    assert backup.list_backups(d) == []
    db.expire_all()
    assert db.query(AuditLog).filter(AuditLog.action.in_(["backup_create", "backup_delete"])).count() == 2


def test_restore_replaces_database_and_keeps_signature_blob(browser, db, users, settings, tmp_path):
    d = _dir(settings, tmp_path)
    admin, alice = browser("admin"), browser("alice")
    admin.post("/admin/settings", {"show_amounts": "1", "til_enabled": "1", "signature_required": "1"})
    alice.post("/oncall/2026/8", {"oc_2026-08-10": "1", "action": "save"})
    alice.post("/overtime/entries", _ot("2026-08-10", "18:00", "19:30"))
    alice.post("/submit/2026/8", {"signature": SIG, "signature_confirm": "1"})
    admin.post("/admin/backup/create")
    name = backup.list_backups(d)[0].name

    # change the data after the backup: delete the sheets and add a user
    with SessionLocal() as s:
        for sheet in s.query(Sheet).all():
            s.delete(sheet)
        s.add(User(username="zed", first_name="Z", last_name="Z", is_active=True))
        s.commit()

    admin.post(f"/admin/backup/restore/{name}", {"confirm": "no"})
    with SessionLocal() as s:
        assert s.query(User).filter_by(username="zed").count() == 1  # not confirmed: nothing happened

    admin.post(f"/admin/backup/restore/{name}", {"confirm": "RESTORE"})
    with SessionLocal() as s:
        assert s.query(User).filter_by(username="zed").count() == 0
        assert s.query(Sheet).count() == 2 and s.query(OvertimeEntry).count() == 1
        sig = s.query(Signature).first()
        assert sig is not None and sig.image_png.startswith(b"\x89PNG")
        assert s.query(AuditLog).filter_by(action="backup_restore").count() == 1
        # new rows keep working after the restore (ids continue)
        s.add(User(username="after", first_name="A", last_name="A", is_active=True))
        s.commit()
    assert any(b.kind == "pre-restore" for b in backup.list_backups(d))


def test_restore_keeps_manager_hierarchy(browser, db, users, settings, tmp_path):
    d = _dir(settings, tmp_path)
    admin = browser("admin")
    admin.post("/admin/backup/create")
    name = backup.list_backups(d)[0].name
    admin.post(f"/admin/backup/restore/{name}", {"confirm": "RESTORE"})
    with SessionLocal() as s:
        alice = s.query(User).filter_by(username="alice").one()
        assert alice.manager.username == "manager" and alice.manager.manager.username == "head"


def test_upload_validation_and_rekey(browser, users, settings, tmp_path):
    d = _dir(settings, tmp_path)
    admin = browser("admin")
    admin.post("/admin/backup/upload", files={"file": ("x.tlbk", b"not encrypted")})
    plain = gzip.compress(json.dumps({"format": backup.FORMAT}).encode())
    admin.post("/admin/backup/upload", files={"file": ("x.tlbk", plain)})  # unencrypted files are refused
    bad = {"format": backup.FORMAT, "version": 1, "tables": {"nope": {"columns": [], "rows": []}}}
    admin.post("/admin/backup/upload", files={"file": ("x.tlbk", backup.encrypt(gzip.compress(json.dumps(bad).encode()), backup.passphrase_of(settings)))})
    assert backup.list_backups(d) == []

    admin.post("/admin/backup/create")
    good = Path(d, backup.list_backups(d)[0].name).read_bytes()
    admin.post("/admin/backup/upload", files={"file": ("x.tlbk", good)})
    assert sorted(b.kind for b in backup.list_backups(d)) == ["manual", "uploaded"]

    # a file made with another server's passphrase needs that passphrase and is re-encrypted with ours
    foreign = backup.encrypt(gzip.compress(Path(d, backup.list_backups(d)[0].name).read_bytes() and
                                           json.dumps(backup.load_payload(good, backup.passphrase_of(settings))).encode()), "other-server-key")
    admin.post("/admin/backup/upload", files={"file": ("y.tlbk", foreign)})
    assert len(backup.list_backups(d)) == 2  # refused without the passphrase
    admin.post("/admin/backup/upload", data={"source_passphrase": "other-server-key"}, files={"file": ("y.tlbk", foreign)})
    assert len(backup.list_backups(d)) == 3
    newest = Path(d, backup.list_backups(d)[0].name).read_bytes()
    assert backup.load_payload(newest, backup.passphrase_of(settings))["format"] == backup.FORMAT


def test_legacy_plain_backup_gets_encrypted(settings, tmp_path):
    d = _dir(settings, tmp_path)
    Path(d).mkdir(parents=True)
    (Path(d) / "timelog-20260101-000001.json.gz").write_bytes(gzip.compress(b"{}"))
    assert backup.encrypt_legacy(d, "k") == 1
    assert [b.name for b in backup.list_backups(d)] == ["timelog-20260101-000001.tlbk"]
    assert backup.decrypt((Path(d) / "timelog-20260101-000001.tlbk").read_bytes(), "k")


def test_schedule_saved_and_validated(browser, db, users, settings, tmp_path):
    _dir(settings, tmp_path)
    admin = browser("admin")
    admin.post("/admin/backup/schedule", {"enabled": "1", "frequency": "weekly", "weekday": "4", "time": "03:30", "keep": "7"})
    db.expire_all()
    sch = backup.load_schedule(db)
    assert (sch.enabled, sch.frequency, sch.weekday, sch.minute, sch.keep) == (True, "weekly", 4, 210, 7)
    admin.post("/admin/backup/schedule", {"frequency": "monthly", "weekday": "4", "time": "03:30", "keep": "7"})
    admin.post("/admin/backup/schedule", {"frequency": "daily", "weekday": "0", "time": "25:00", "keep": "7"})
    admin.post("/admin/backup/schedule", {"frequency": "daily", "weekday": "0", "time": "03:00", "keep": "0"})
    db.expire_all()
    assert backup.load_schedule(db).frequency == "weekly"  # invalid submissions changed nothing
    admin.post("/admin/backup/schedule", {"frequency": "daily", "weekday": "0", "time": "03:00", "keep": "5"})  # off
    db.expire_all()
    assert backup.load_schedule(db).enabled is False
    assert "Scheduled backups are" in admin.get("/admin/backup").text


def test_schedule_due_logic():
    daily = backup.Schedule(frequency="daily", minute=2 * 60, since=dt.datetime(2026, 10, 1))
    now = dt.datetime(2026, 10, 29, 9, 0)  # a Thursday
    assert backup.occurrence_before(now, daily) == dt.datetime(2026, 10, 29, 2, 0)
    assert backup.next_occurrence(now, daily) == dt.datetime(2026, 10, 30, 2, 0)
    assert backup.is_due(now, daily, last_auto=None)
    assert backup.is_due(now, daily, last_auto=dt.datetime(2026, 10, 28, 2, 0, 5))
    assert not backup.is_due(now, daily, last_auto=dt.datetime(2026, 10, 29, 2, 0, 5))
    assert not backup.is_due(dt.datetime(2026, 10, 29, 1, 0), daily, last_auto=dt.datetime(2026, 10, 28, 2, 0, 5))
    # saving the schedule at 15:00 does not trigger an immediate catch-up run of today's 02:00 slot
    fresh = backup.Schedule(frequency="daily", minute=120, since=dt.datetime(2026, 10, 29, 15, 0))
    assert not backup.is_due(dt.datetime(2026, 10, 29, 16, 0), fresh, None)
    assert backup.is_due(dt.datetime(2026, 10, 30, 2, 1), fresh, None)
    # weekly: Friday 18:00
    weekly = backup.Schedule(frequency="weekly", weekday=4, minute=18 * 60, since=dt.datetime(2026, 1, 1))
    assert backup.occurrence_before(now, weekly) == dt.datetime(2026, 10, 23, 18, 0)
    assert backup.next_occurrence(now, weekly) == dt.datetime(2026, 10, 30, 18, 0)
    assert not backup.is_due(now, backup.Schedule(enabled=False), None)


def test_prune_only_removes_old_automatic_backups(browser, users, settings, tmp_path):
    d = _dir(settings, tmp_path)
    Path(d).mkdir(parents=True)
    for stamp, label in (("20260101-000001", "-auto"), ("20260102-000001", "-auto"), ("20260103-000001", "-auto"),
                         ("20260101-000002", ""), ("20260101-000003", "-pre")):
        (Path(d) / f"timelog-{stamp}{label}.tlbk").write_bytes(b"x")
    assert backup.prune_auto(d, 2) == 1
    names = [b.name for b in backup.list_backups(d)]
    assert "timelog-20260101-000001-auto.json.gz" not in names and len(names) == 4
