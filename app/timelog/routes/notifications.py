from fastapi import BackgroundTasks

from ..calendar_utils import month_label
from ..config import get_settings
from ..models import APPROVED, KIND_ONCALL, Sheet
from ..notify import send_mail
from .common import sheet_url


def _kind(sheet: Sheet) -> str:
    return "on-call" if sheet.kind == KIND_ONCALL else "overtime"


def notify_submitted(background: BackgroundTasks, sheet: Sheet) -> None:
    s = get_settings()
    manager = sheet.user.manager
    if manager is None or not manager.email:
        return
    body = (f"{sheet.user.full_name} submitted the {_kind(sheet)} sheet for "
            f"{month_label(sheet.year, sheet.month)}.\n\nReview it: {s.base_url}{sheet_url(sheet)}\n")
    background.add_task(send_mail, s, manager.email, f"{_kind(sheet).capitalize()} sheet to validate", body)


def notify_decided(background: BackgroundTasks, sheet: Sheet) -> None:
    s = get_settings()
    if not sheet.user.email:
        return
    verdict = "approved" if sheet.status == APPROVED else "rejected"
    body = (f"Your {_kind(sheet)} sheet for {month_label(sheet.year, sheet.month)} was {verdict}"
            f"{' by ' + sheet.decided_by.full_name if sheet.decided_by else ''}.\n")
    if sheet.decision_comment:
        body += f"\nComment: {sheet.decision_comment}\n"
    if sheet.status == APPROVED and sheet.is_late:
        body += (f"\nIt was approved after the Finance cut-off: it will be paid with the "
                 f"{month_label(sheet.payroll_year, sheet.payroll_month)} payroll, not with "
                 f"{month_label(sheet.year, sheet.month)}.\n")
    body += f"\n{s.base_url}{sheet_url(sheet, sheet.user)}\n"
    background.add_task(send_mail, s, sheet.user.email, f"{_kind(sheet).capitalize()} sheet {verdict}", body)


def notify_payroll_sent(background: BackgroundTasks, run) -> None:
    """E-mail every employee whose approved sheets were part of a payroll run sent to Finance."""
    s = get_settings()
    seen = set()
    for item in run.items:
        user = item.sheet.user
        if user.id in seen or not user.email:
            continue
        seen.add(user.id)
        body = (f"Your approved on-call / overtime declarations for the {month_label(run.year, run.month)} payroll were sent to "
                f"Finance for payment on {run.sent_at:%d/%m/%Y}.\n")
        background.add_task(send_mail, s, user.email, "Declarations sent to Finance for payment", body)
