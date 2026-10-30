from fastapi import APIRouter, Depends, Request
from sqlalchemy.orm import Session

from ..app_settings import oncall_week
from ..backup import WEEKDAYS
from ..config import get_settings
from ..db import get_db
from ..models import User
from ..security import current_user
from ..web import render

router = APIRouter()


@router.get("/guide")
def guide(request: Request, me: User = Depends(current_user), db: Session = Depends(get_db)):
    day, handover = oncall_week(db, get_settings())
    return render(request, "guide.html", {
        "week_start_name": WEEKDAYS[day], "week_end_name": WEEKDAYS[(day + 6) % 7], "week_handover": handover,
    })
