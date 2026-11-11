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


class MailMaybeSent(MailError):
    """The server may have taken the message, but its answer was lost (issue #118).

    Sending again could email the client twice, so the caller counts the email
    as sent and warns the user.
    """


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


class _StagedSMTP(smtplib.SMTP):
    """An SMTP client that notes how far the DATA stage got (issue #118)."""

    data_started = False
    data_reply: int | None = None

    def data(self, msg):
        self.data_started = True
        code, reply = super().data(msg)
        self.data_reply = code
        return code, reply


def _server_may_have_it(smtp: _StagedSMTP | None, exc: Exception) -> bool:
    """Whether the server may have taken the message when `exc` was raised.

    Before DATA (connect, STARTTLS, login, MAIL FROM, RCPT TO) nothing was
    handed over. A refused DATA command, or a 4xx or 5xx answer to the message,
    means the server turned it down. Anything else once DATA began, such as a
    timeout or a dropped connection while waiting for the answer, may have
    sent it (issue #118).
    """
    if smtp is None or not smtp.data_started:
        return False
    if isinstance(exc, smtplib.SMTPDataError):
        return smtp.data_reply is not None and not 400 <= smtp.data_reply < 600
    return True


class SmtpMailer:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def send(self, to: str, subject: str, body: str) -> None:
        """Send one email. Raise MailMaybeSent if the server may have it (issue #118)."""
        settings = self.settings
        if not settings.smtp_host or not settings.smtp_from:
            raise MailError("Email is not configured. Set SMTP_HOST and SMTP_FROM.")
        message = EmailMessage()
        message["From"] = settings.smtp_from
        message["To"] = to
        message["Subject"] = subject
        message.set_content(body)
        smtp: _StagedSMTP | None = None
        try:
            smtp = _StagedSMTP(settings.smtp_host, settings.smtp_port, timeout=settings.smtp_timeout_seconds)
            if settings.smtp_starttls:
                smtp.starttls()
            if settings.smtp_username:
                smtp.login(settings.smtp_username, settings.smtp_password)
            smtp.send_message(message)
        except (smtplib.SMTPException, OSError) as exc:
            if smtp is not None:
                smtp.close()
            name = exc.__class__.__name__
            if _server_may_have_it(smtp, exc):
                raise MailMaybeSent(f"The mail server did not confirm the email ({name}) and may still deliver it.") from exc
            raise MailError(f"The email could not be sent: {name}. Try again.") from exc
        # The server answered 250 to the message, so it has it. A failed QUIT
        # does not change that.
        try:
            smtp.quit()
        except (smtplib.SMTPException, OSError):
            smtp.close()


def build_mailer(settings: Settings) -> Mailer:
    if settings.email_mode == "smtp":
        return SmtpMailer(settings)
    return MockMailer()
