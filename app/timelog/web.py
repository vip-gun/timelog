"""Template rendering helpers shared by all routes."""
import datetime as dt
import re
from decimal import Decimal
from pathlib import Path
from typing import Optional
from urllib.parse import urlencode

from fastapi import Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates
from markupsafe import Markup

from .calendar_utils import MONTH_NAMES, fmt_minutes, month_label, shift_month
from .config import get_settings
from .models import DAY_TYPE_LABELS
from .security import csrf_token, pop_flashes
from .signatures import STATEMENT as SIGNATURE_STATEMENT, verify as verify_signature
from .version import BUILD

BASE_DIR = Path(__file__).parent
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))


def _money(value) -> str:
    return f"{Decimal(value or 0):,.2f}"


def _hours(value) -> str:
    return f"{Decimal(value or 0):.2f}"


def _date(value: Optional[dt.date]) -> str:
    return value.strftime("%d/%m/%Y") if value else ""


def _datetime(value: Optional[dt.datetime]) -> str:
    return value.strftime("%d/%m/%Y %H:%M") + " UTC" if value else ""


def _titlecase(value) -> Markup:
    """Capitalise the first letter of every word, leaving the rest as written ("On-call" stays "On-call")."""
    return Markup(re.sub(r"(^|\s)([a-z])", lambda m: m.group(1) + m.group(2).upper(), str(value)))


templates.env.filters.update(titlecase=_titlecase, money=_money, hours=_hours, date=_date, datetime=_datetime, hhmm=fmt_minutes)
templates.env.globals.update(
    DAY_TYPE_LABELS=DAY_TYPE_LABELS, MONTH_NAMES=MONTH_NAMES, month_label=month_label, shift_month=shift_month,
    build=BUILD, SIGNATURE_STATEMENT=SIGNATURE_STATEMENT, verify_signature=verify_signature,
)


def render(request: Request, name: str, ctx: Optional[dict] = None, status_code: int = 200):
    context = dict(ctx or {})
    context.update(
        user=getattr(request.state, "user", None),
        is_manager=getattr(request.state, "is_manager", False),
        features=getattr(request.state, "features", {"til": True, "signature": False}),
        csrf=csrf_token(request),
        payroll_cutoff=getattr(request.state, "payroll_cutoff", get_settings().payroll_cutoff_day),
        notices=getattr(request.state, "notices", []),
        flashes=pop_flashes(request),
        settings=get_settings(),
        today=dt.date.today(),
    )
    return templates.TemplateResponse(request, name, context, status_code=status_code)


def redirect(path: str, **params) -> RedirectResponse:
    params = {k: v for k, v in params.items() if v not in (None, "")}
    url = path + ("?" + urlencode(params) if params else "")
    return RedirectResponse(url, status_code=303)
