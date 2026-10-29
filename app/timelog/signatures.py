"""Signatures of submitted sheets: validation of the drawn image and a digest of the signed content.

The digest (SHA-256 over a canonical JSON of the sheet) lets a manager verify that what is displayed is
exactly what the employee signed: any later change to the sheet lines makes `verify()` fail.
"""
import base64
import binascii
import hashlib
import json
import struct
from decimal import Decimal

from .models import KIND_ONCALL, Sheet

STATEMENT = ("I confirm that this declaration is complete and accurate, and I submit it to my line manager "
             "for validation.")
DATA_URL_PREFIX = "data:image/png;base64,"
MAX_PNG_BYTES = 300 * 1024
MAX_DIMENSION = 2000
PNG_MAGIC = b"\x89PNG\r\n\x1a\n"


class SignatureError(ValueError):
    pass


def decode_png_data_url(value: str) -> bytes:
    """Validate a `data:image/png;base64,...` value produced by the signature pad and return the PNG bytes."""
    value = (value or "").strip()
    if not value:
        raise SignatureError("Please sign in the signature box before submitting.")
    if not value.startswith(DATA_URL_PREFIX):
        raise SignatureError("Invalid signature format.")
    payload = value[len(DATA_URL_PREFIX):]
    if len(payload) > MAX_PNG_BYTES * 4 // 3 + 4:
        raise SignatureError("Signature image too large.")
    try:
        png = base64.b64decode(payload, validate=True)
    except (binascii.Error, ValueError):
        raise SignatureError("Invalid signature data.")
    if not png.startswith(PNG_MAGIC) or len(png) < 33 or png[12:16] != b"IHDR":
        raise SignatureError("Invalid signature image.")
    width, height = struct.unpack(">II", png[16:24])
    if not (0 < width <= MAX_DIMENSION and 0 < height <= MAX_DIMENSION):
        raise SignatureError("Invalid signature image size.")
    return png


def _num(value) -> str:
    """Fixed two-decimal text: Decimal('0'), Decimal('0.0') and Decimal('0.00') (in-memory vs reloaded from
    the database) must produce the same digest."""
    return f"{Decimal(value):.2f}"


def sheet_content(sheet: Sheet) -> dict:
    """Canonical representation of what is being signed."""
    base = {
        "user": sheet.user.username, "kind": sheet.kind, "year": sheet.year, "month": sheet.month,
        "seq": sheet.seq, "employee_comment": sheet.employee_comment,
    }
    if sheet.kind == KIND_ONCALL:
        base["days"] = [
            [d.date.isoformat(), d.day_type, d.intervention, _num(d.rate), _num(d.amount), d.incident_ref, d.comment]
            for d in sorted(sheet.oncall_days, key=lambda d: d.date)
        ]
    else:
        base["entries"] = [
            [e.date.isoformat(), e.start_minute, e.end_minute, e.day_type, _num(e.hours), _num(e.night_hours),
             e.compensation, _num(e.til_credit), e.on_call_intervention, e.preapproved, e.incident_ref, e.description]
            for e in sorted(sheet.overtime_entries, key=lambda e: (e.date, e.start_minute))
        ]
    return base


def sheet_digest(sheet: Sheet) -> str:
    raw = json.dumps(sheet_content(sheet), sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(raw.encode()).hexdigest()


def verify(sheet: Sheet) -> bool:
    sig = sheet.signature
    return sig is not None and sig.content_digest == sheet_digest(sheet)
