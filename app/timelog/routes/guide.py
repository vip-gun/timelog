import datetime as dt

from fastapi import APIRouter, Depends, Request
from sqlalchemy.orm import Session

from ..app_settings import cutoff_schedule, oncall_week
from ..backup import WEEKDAYS
from ..config import get_settings
from ..db import get_db
from ..models import User
from ..security import current_user
from ..web import render

router = APIRouter()


@router.get("/guide")
def guide(request: Request, me: User = Depends(current_user), db: Session = Depends(get_db)):
    base = get_settings()
    day, handover = oncall_week(db, base)
    today = dt.date.today()
    return render(request, "guide.html", {
        "cutoffs": cutoff_schedule(db, base, *(today.year, today.month - 1) if today.month > 1 else (today.year - 1, 12), 5),
        "week_start_name": WEEKDAYS[day], "week_end_name": WEEKDAYS[(day + 6) % 7], "week_handover": handover,
    })
