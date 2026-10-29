"""One submission for the whole month (on-call + overtime) and the user's saved signature picture."""
from urllib.parse import urlsplit

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request
from fastapi.responses import Response
from sqlalchemy.orm import Session

from .. import workflow
from ..config import get_settings
from ..db import get_db
from ..models import SavedSignature, User, utcnow
from ..security import current_user, flash, verify_csrf
from ..web import redirect
from .common import check_month, signature_from_form
from .notifications import notify_submitted

router = APIRouter()


def save_signature_picture(db: Session, user: User, png: bytes) -> None:
    saved = db.get(SavedSignature, user.id)
    if saved is None:
        db.add(SavedSignature(user_id=user.id, image_png=png))
    else:
        saved.image_png, saved.updated_at = png, utcnow()


def submitted_message(sheets) -> str:
    kinds = " and ".join(sorted({"on-call" if s.kind == "oncall" else "overtime" for s in sheets}))
    return f"Your {kinds} declaration was submitted to your line manager for validation."


@router.post("/submit/{year}/{month}", dependencies=[Depends(verify_csrf)])
async def submit_month(request: Request, year: int, month: int, background: BackgroundTasks,
                       me: User = Depends(current_user), db: Session = Depends(get_db)):
    check_month(year, month)
    form = await request.form()
    back = str(form.get("back", "overtime"))
    url = f"/{'oncall' if back == 'oncall' else 'overtime'}/{year}/{month}"
    try:
        signature = signature_from_form(request, form, db)
        sheets = workflow.submit_month(db, get_settings(), me, year, month,
                                       str(form.get("employee_comment", "")), signature)
        if signature is not None and form.get("save_signature"):
            save_signature_picture(db, me, signature.png)
        db.commit()
    except workflow.WorkflowError as exc:
        db.rollback()
        flash(request, str(exc), "error")
        return redirect(url)
    flash(request, submitted_message(sheets), "success")
    for sheet in sheets:
        notify_submitted(background, sheet)
    return redirect(url)


@router.get("/signature/saved.png")
def saved_signature(me: User = Depends(current_user), db: Session = Depends(get_db)):
    saved = db.get(SavedSignature, me.id)
    if saved is None:
        raise HTTPException(status_code=404, detail="Not found")
    return Response(saved.image_png, media_type="image/png", headers={"Cache-Control": "private, no-store"})


@router.post("/signature/forget", dependencies=[Depends(verify_csrf)])
def forget_signature(request: Request, me: User = Depends(current_user), db: Session = Depends(get_db)):
    saved = db.get(SavedSignature, me.id)
    if saved is not None:
        db.delete(saved)
        db.commit()
    flash(request, "Your saved signature was deleted.", "success")
    path = urlsplit(request.headers.get("referer", "")).path
    return redirect(path if path.startswith("/") and not path.startswith("//") else "/")
