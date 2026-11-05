"""Email gateway for reminders (issue #26).

Email is a second reminder channel beside PayPal. Like the PayPal gateway
(ADR 0001), it is a small protocol with a mock mailer that keeps the demo free
of credentials and an SMTP mailer for real mail. Only an approved reminder
reaches it (ADR 0003).
"""

from __future__ import annotations

import smtplib
from email.message import EmailMessage
from typing import Protocol

from app.config import Settings


class MailError(RuntimeError):
    """A send that failed, with a message the user can read."""


class Mailer(Protocol):
    def send(self, to: str, subject: str, body: str) -> None: ...


class MockMailer:
    def __init__(self) -> None:
        self.outbox: list[dict] = []
        self.fail_with: str | None = None

    def send(self, to: str, subject: str, body: str) -> None:
        if self.fail_with:
            raise MailError(self.fail_with)
        self.outbox.append({"to": to, "subject": subject, "body": body})


class SmtpMailer:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def send(self, to: str, subject: str, body: str) -> None:
        settings = self.settings
        if not settings.smtp_host or not settings.smtp_from:
            raise MailError("Email is not configured. Set SMTP_HOST and SMTP_FROM.")
        message = EmailMessage()
        message["From"] = settings.smtp_from
        message["To"] = to
        message["Subject"] = subject
        message.set_content(body)
        try:
            with smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=settings.smtp_timeout_seconds) as smtp:
                if settings.smtp_starttls:
                    smtp.starttls()
                if settings.smtp_username:
                    smtp.login(settings.smtp_username, settings.smtp_password)
                smtp.send_message(message)
        except (smtplib.SMTPException, OSError) as exc:
            raise MailError(f"The email could not be sent: {exc.__class__.__name__}. Try again.") from exc


def build_mailer(settings: Settings) -> Mailer:
    if settings.email_mode == "smtp":
        return SmtpMailer(settings)
    return MockMailer()
