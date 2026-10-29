"""Optional e-mail notifications through an internal SMTP relay (disabled when SMTP_HOST is empty)."""
import logging
import smtplib
from email.message import EmailMessage

from .config import Settings

log = logging.getLogger(__name__)


def send_mail(s: Settings, to: str, subject: str, body: str) -> None:
    if not (s.smtp_host and s.smtp_from and to):
        return
    msg = EmailMessage()
    msg["From"], msg["To"], msg["Subject"] = s.smtp_from, to, f"[{s.app_name}] {subject}"
    msg.set_content(body)
    try:
        with smtplib.SMTP(s.smtp_host, s.smtp_port, timeout=15) as smtp:
            if s.smtp_starttls:
                smtp.starttls()
            if s.smtp_user:
                smtp.login(s.smtp_user, s.smtp_password)
            smtp.send_message(msg)
    except (OSError, smtplib.SMTPException):
        log.exception("Could not send notification to %s", to)
