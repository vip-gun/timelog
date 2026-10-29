from fastapi import APIRouter, BackgroundTasks, Depends, Form, HTTPException, Request
from fastapi.responses import Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import calc, workflow
from ..access import require_view_sheet
from ..config import get_settings
from ..db import get_db
from ..models import KIND_ONCALL, SUBMITTED, Sheet, Signature, User
from ..security import current_user, flash, verify_csrf
from ..web import redirect, render
from .common import conflicts_for_viewer, sheet_url
from .notifications import notify_decided

router = APIRouter()


def sheet_summary(sheet: Sheet) -> str:
    if sheet.kind == KIND_ONCALL:
        amount = workflow.oncall_total(sheet)
        return f"{len(sheet.oncall_days)} day(s) - {amount:,.2f}"
    t = calc.overtime_totals(sheet.overtime_entries)
    text = f"{t.total_hours:.2f} h" + (f" ({t.paid_hours:.2f} paid, {t.til_hours:.2f} TIL)" if t.til_hours else "")
    return text + (f" - supplementary #{sheet.seq}" if sheet.seq > 1 else "")


@router.get("/approvals")
def approvals(request: Request, me: User = Depends(current_user), db: Session = Depends(get_db)):
    q = select(Sheet).join(User, Sheet.user_id == User.id).where(Sheet.status == SUBMITTED, Sheet.user_id != me.id)
    if not me.is_admin:
        q = q.where(User.manager_id == me.id)
    pending = list(db.scalars(q.order_by(Sheet.submitted_at)))
    recent = list(db.scalars(
        select(Sheet).where(Sheet.decided_by_id == me.id).order_by(Sheet.decided_at.desc()).limit(20)))
    conflicts = {s.id: conflicts_for_viewer(db, me, s) for s in pending if s.kind == KIND_ONCALL}
    return render(request, "approvals.html", {
        "conflicts": {sid: c for sid, c in conflicts.items() if c},
        "pending": [(s, sheet_summary(s), sheet_url(s, me)) for s in pending],
        "recent": [(s, sheet_summary(s), sheet_url(s, me)) for s in recent],
    })


@router.get("/sheets/{sheet_id}")
def open_sheet(sheet_id: int, me: User = Depends(current_user), db: Session = Depends(get_db)):
    return redirect(sheet_url(require_view_sheet(db, me, sheet_id), me))


@router.post("/sheets/{sheet_id}/decide", dependencies=[Depends(verify_csrf)])
def decide(request: Request, sheet_id: int, background: BackgroundTasks, action: str = Form(...),
           comment: str = Form(""), me: User = Depends(current_user), db: Session = Depends(get_db)):
    sheet = require_view_sheet(db, me, sheet_id)
    try:
        workflow.decide(db, get_settings(), sheet, me, approve=(action == "approve"), comment=comment)
        db.commit()
    except workflow.WorkflowError as exc:
        db.rollback()
        flash(request, str(exc), "error")
        return redirect(sheet_url(sheet, me))
    flash(request, f"Sheet of {sheet.user.full_name} {sheet.status}.", "success")
    notify_decided(background, sheet)
    return redirect("/approvals")


@router.post("/sheets/{sheet_id}/reopen", dependencies=[Depends(verify_csrf)])
def reopen(request: Request, sheet_id: int, reason: str = Form(""),
           me: User = Depends(current_user), db: Session = Depends(get_db)):
    sheet = require_view_sheet(db, me, sheet_id)
    try:
        workflow.reopen(db, sheet, me, reason)
        db.commit()
        flash(request, "Sheet reopened as draft.", "success")
    except workflow.WorkflowError as exc:
        db.rollback()
        flash(request, str(exc), "error")
    return redirect(sheet_url(sheet, me))


def _own_sheet(db: Session, me: User, sheet_id: int) -> Sheet:
    sheet = require_view_sheet(db, me, sheet_id)
    if sheet.user_id != me.id:
        raise workflow.WorkflowError("Only the owner can do this.")
    return sheet


@router.post("/sheets/{sheet_id}/withdraw", dependencies=[Depends(verify_csrf)])
def withdraw(request: Request, sheet_id: int, me: User = Depends(current_user), db: Session = Depends(get_db)):
    sheet = require_view_sheet(db, me, sheet_id)
    try:
        workflow.withdraw(db, _own_sheet(db, me, sheet_id), me)
        db.commit()
        flash(request, "Submission withdrawn - the sheet is a draft again.", "success")
    except workflow.WorkflowError as exc:
        db.rollback()
        flash(request, str(exc), "error")
    return redirect(sheet_url(sheet, me))


@router.get("/signatures/{signature_id}.png")
def signature_image(signature_id: int, me: User = Depends(current_user), db: Session = Depends(get_db)):
    """Signature image - same visibility as the signed sheet (owner, managers above, admins)."""
    sig = db.get(Signature, signature_id)
    if sig is None:
        raise HTTPException(status_code=404, detail="Not found")
    require_view_sheet(db, me, sig.sheet_id)
    return Response(sig.image_png, media_type="image/png", headers={"Cache-Control": "private, no-store"})
