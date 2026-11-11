"""A reminder email goes to the client at most once, even if the server's reply is lost (issue #118).

A small fake SMTP server runs in a thread, so the real smtplib client talks to
it and fails at the stage each test picks.
"""

import socket
import socketserver
import threading
from datetime import timedelta
from decimal import Decimal

import pytest
from sqlalchemy import create_engine, text
from sqlmodel import Session

from app.config import Settings
from app.mailer import MailError, MailMaybeSent, SmtpMailer
from app.models import Reminder, User, init_db, make_engine, utcnow
from app.paypal import MockPayPal
from app.reminders import draft_reminders, send_reminder
from app.services import create_invoice
from app.validation import InvoiceProposal

SETTINGS = Settings(database_url="sqlite://", mock_webhooks=True)


class FakeSmtpServer(socketserver.ThreadingTCPServer):
    """Speaks just enough SMTP for smtplib. `mode` picks where it misbehaves."""

    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, mode: str = "ok") -> None:
        super().__init__(("127.0.0.1", 0), FakeSmtpHandler)
        self.mode = mode
        self.messages: list[bytes] = []

    @property
    def port(self) -> int:
        return self.server_address[1]


class FakeSmtpHandler(socketserver.StreamRequestHandler):
    def reply(self, line: str) -> None:
        self.wfile.write(line.encode() + b"\r\n")

    def handle(self) -> None:
        mode = self.server.mode
        self.reply("220 fake.test ESMTP")
        while line := self.rfile.readline():
            verb = line.decode().strip().split(" ")[0].upper()
            if verb in ("EHLO", "HELO"):
                self.reply("250 fake.test")
            elif verb == "MAIL":
                self.reply("250 OK")
            elif verb == "RCPT":
                self.reply("550 No such user" if mode == "refuse_rcpt" else "250 OK")
            elif verb == "DATA":
                self.reply("354 Go ahead")
                body = b""
                while (chunk := self.rfile.readline()) not in (b".\r\n", b""):
                    body += chunk
                self.server.messages.append(body)
                if mode == "hang_after_data":
                    # The server took the message but its answer never comes.
                    self.rfile.read()
                    return
                self.reply("554 Rejected" if mode == "reject_data" else "250 Queued")
            elif verb == "RSET":
                self.reply("250 OK")
            elif verb == "QUIT":
                self.reply("500 Odd" if mode == "bad_quit" else "221 Bye")
                return
            else:
                self.reply("502 Not implemented")


@pytest.fixture
def smtp_server(request):
    servers = []

    def start(mode: str = "ok") -> FakeSmtpServer:
        server = FakeSmtpServer(mode)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        servers.append(server)
        return server

    yield start
    for server in servers:
        server.shutdown()
        server.server_close()


def smtp_settings(port: int) -> Settings:
    return Settings(
        database_url="sqlite://",
        mock_webhooks=True,
        email_mode="smtp",
        smtp_host="127.0.0.1",
        smtp_port=port,
        smtp_from="me@duebell.test",
        smtp_starttls=False,
        smtp_timeout_seconds=1.0,
    )


def closed_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@pytest.fixture
def session():
    engine = make_engine("sqlite://")
    init_db(engine)
    with Session(engine) as session:
        yield session


def pending_reminder(session, gateway) -> Reminder:
    user = User(email="owner@duebell.test", password_hash="x")
    session.add(user)
    session.commit()
    proposal = InvoiceProposal(
        client_name="Acme Studio",
        client_email="billing@acme.test",
        description="Logo design",
        amount=Decimal("1200.00"),
        currency="USD",
        terms_days=14,
    )
    invoice = create_invoice(session, proposal, SETTINGS, gateway, user_id=user.id)
    invoice.due_at = utcnow() - timedelta(days=3)
    session.add(invoice)
    session.commit()
    return draft_reminders(session, SETTINGS, scope=user.id)[0]


# The mailer on its own: which failures mean the server may have the message.


def test_the_mailer_sends_through_smtp(smtp_server):
    server = smtp_server()
    SmtpMailer(smtp_settings(server.port)).send("billing@acme.test", "Reminder", "Please pay.")
    assert len(server.messages) == 1


def test_a_refused_connection_is_a_plain_error():
    with pytest.raises(MailError) as caught:
        SmtpMailer(smtp_settings(closed_port())).send("billing@acme.test", "Reminder", "Please pay.")
    assert not isinstance(caught.value, MailMaybeSent)


def test_a_refused_recipient_is_a_plain_error(smtp_server):
    server = smtp_server("refuse_rcpt")
    with pytest.raises(MailError) as caught:
        SmtpMailer(smtp_settings(server.port)).send("billing@acme.test", "Reminder", "Please pay.")
    assert not isinstance(caught.value, MailMaybeSent)
    assert server.messages == []


def test_a_message_the_server_rejects_is_a_plain_error(smtp_server):
    server = smtp_server("reject_data")
    with pytest.raises(MailError) as caught:
        SmtpMailer(smtp_settings(server.port)).send("billing@acme.test", "Reminder", "Please pay.")
    assert not isinstance(caught.value, MailMaybeSent)


def test_a_lost_reply_after_data_may_have_sent(smtp_server):
    server = smtp_server("hang_after_data")
    with pytest.raises(MailMaybeSent):
        SmtpMailer(smtp_settings(server.port)).send("billing@acme.test", "Reminder", "Please pay.")
    assert len(server.messages) == 1


def test_a_bad_quit_after_the_server_took_the_message_is_a_send(smtp_server):
    server = smtp_server("bad_quit")
    SmtpMailer(smtp_settings(server.port)).send("billing@acme.test", "Reminder", "Please pay.")
    assert len(server.messages) == 1


# Approval: an email attempt is saved before the send and decides a retry.


def test_the_normal_path_records_the_attempt_and_sends_once(session, smtp_server):
    server = smtp_server()
    gateway = MockPayPal(True)
    reminder = pending_reminder(session, gateway)
    outcome = send_reminder(session, reminder, gateway, SmtpMailer(smtp_settings(server.port)), "email")
    assert outcome.reminder.status == "SENT"
    assert outcome.reminder.sent_channels == "email"
    assert outcome.reminder.email_attempted_at is not None
    assert outcome.warnings == []
    assert len(server.messages) == 1


@pytest.mark.parametrize("mode", ["no_server", "refuse_rcpt"])
def test_an_error_before_the_server_took_it_leaves_email_free_to_retry(session, smtp_server, mode):
    port = closed_port() if mode == "no_server" else smtp_server(mode).port
    gateway = MockPayPal(True)
    reminder = pending_reminder(session, gateway)

    with pytest.raises(MailError):
        send_reminder(session, reminder, gateway, SmtpMailer(smtp_settings(port)), "email")
    session.refresh(reminder)
    assert reminder.status == "PENDING"
    assert reminder.email_attempted_at is None
    assert reminder.sent_channels == ""

    server = smtp_server()
    outcome = send_reminder(session, reminder, gateway, SmtpMailer(smtp_settings(server.port)), "email")
    assert outcome.reminder.sent_channels == "email"
    assert outcome.warnings == []
    assert len(server.messages) == 1


def test_a_timeout_after_data_counts_the_email_as_sent(session, smtp_server):
    server = smtp_server("hang_after_data")
    gateway = MockPayPal(True)
    reminder = pending_reminder(session, gateway)

    outcome = send_reminder(session, reminder, gateway, SmtpMailer(smtp_settings(server.port)), "both")
    assert outcome.reminder.status == "SENT"
    assert outcome.reminder.sent_channels == "paypal,email"
    assert any("may still deliver it" in warning for warning in outcome.warnings)
    assert len(server.messages) == 1
    assert len(gateway.sent_reminders) == 1


def test_a_retry_after_an_email_attempt_does_not_email_again(session, smtp_server):
    """The attempt was saved, then the process stopped before the result was."""
    server = smtp_server()
    gateway = MockPayPal(True)
    reminder = pending_reminder(session, gateway)
    reminder.email_attempted_at = utcnow()
    session.add(reminder)
    session.commit()

    outcome = send_reminder(session, reminder, gateway, SmtpMailer(smtp_settings(server.port)), "email")
    assert server.messages == []
    assert outcome.reminder.status == "SENT"
    assert outcome.reminder.sent_channels == "email"
    assert any("Check your sent mail" in warning for warning in outcome.warnings)


def test_an_older_database_gains_the_email_attempt_column(tmp_path):
    url = f"sqlite:///{tmp_path / 'old.db'}"
    engine = make_engine(url)
    init_db(engine)
    with engine.begin() as connection:
        connection.execute(text('ALTER TABLE "reminder" DROP COLUMN "email_attempted_at"'))
    init_db(create_engine(url))
    with engine.connect() as connection:
        columns = {row[1] for row in connection.execute(text('PRAGMA table_info("reminder")'))}
    assert "email_attempted_at" in columns
