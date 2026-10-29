import datetime as dt
from typing import Optional

from fastapi import HTTPException, Request
from sqlalchemy.orm import Session

from ..access import can_decide, require_view_user
from ..app_settings import get_flag
from ..calendar_utils import valid_month
from .. import calc, workflow
from ..models import APPROVED, KIND_ONCALL, SETTING_SIGNATURE_REQUIRED, SavedSignature, Sheet, User
from ..signatures import STATEMENT, SignatureError, decode_png_data_url
from ..workflow import SignatureInput, WorkflowError


def check_month(year: int, month: int) -> None:
    if not valid_month(year, month):
        raise HTTPException(status_code=404, detail="Not found")


def resolve_target(db: Session, me: User, user_id: Optional[int]) -> User:
    """The user whose sheet is displayed: self by default, else must be visible to `me`."""
    if not user_id or user_id == me.id:
        return me
    return require_view_user(db, me, user_id)


def sheet_url(sheet: Sheet, viewer: Optional[User] = None) -> str:
    base = f"/{'oncall' if sheet.kind == KIND_ONCALL else 'overtime'}/{sheet.year}/{sheet.month}"
    if viewer is None or viewer.id != sheet.user_id:
        base += f"?user_id={sheet.user_id}"
    return base


def sheet_permissions(me: User, target: User, sheet: Optional[Sheet]) -> dict:
    own = me.id == target.id
    return {
        "own": own,
        "editable": own and (sheet is None or sheet.editable),
        "can_withdraw": own and sheet is not None and sheet.status == "submitted",
        "can_decide": sheet is not None and sheet.status == "submitted" and can_decide(me, sheet),
        "can_reopen": sheet is not None and sheet.status == APPROVED and me.is_admin,
    }


def signature_from_form(request: Request, form, db: Session) -> Optional[SignatureInput]:
    """Signature posted with a submission, or None when signatures are disabled. Raises WorkflowError."""
    if not get_flag(db, SETTING_SIGNATURE_REQUIRED):
        return None
    try:
        png = decode_png_data_url(str(form.get("signature", "")))
    except SignatureError as exc:
        raise WorkflowError(str(exc))
    if not form.get("signature_confirm"):
        raise WorkflowError("Please tick the confirmation statement to sign your declaration.")
    return SignatureInput(png=png, statement=STATEMENT,
                          ip=request.client.host if request.client else "",
                          user_agent=request.headers.get("user-agent", ""))


def submission_context(db: Session, me: User, year: int, month: int) -> dict:
    """What the single month submission would send (drafts with content), for the submit panel."""
    sheets = workflow.month_submittable(db, me, year, month)
    pending = []
    for sheet in sheets:
        if sheet.kind == KIND_ONCALL:
            pending.append({"kind": "oncall", "label": "On-call", "detail": f"{len(sheet.oncall_days)} day(s)"})
        else:
            hours = calc.overtime_totals(sheet.overtime_entries).total_hours
            pending.append({"kind": "overtime", "label": "Overtime" + (f" (supplementary #{sheet.seq})" if sheet.seq > 1 else ""),
                            "detail": f"{hours:.2f} h"})
    return {
        "pending": pending,
        "submit_comment": sheets[0].employee_comment if sheets else "",
        "has_saved_signature": db.get(SavedSignature, me.id) is not None,
    }


def remember_month(request: Request, year: int, month: int) -> None:
    """On-call and overtime pages share the month the user last navigated to."""
    request.session["month"] = [year, month]


def remembered_month(request: Request) -> tuple:
    """(year, month) last viewed, else the current month."""
    try:
        year, month = request.session.get("month", [])
        if valid_month(year, month):
            return year, month
    except (TypeError, ValueError):
        pass
    today = dt.date.today()
    return today.year, today.month
