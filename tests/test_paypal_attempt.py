"""A reminder goes through PayPal at most once, even if the save after it fails (issue #75)."""

from datetime import timedelta
from decimal import Decimal

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.exc import OperationalError
from sqlmodel import Session

from app.config import Settings
from app.mailer import MailError, MockMailer
from app.models import Reminder, User, init_db, make_engine, utcnow
from app.paypal import MockPayPal, PayPalError
from app.reminders import draft_reminders, reminder_rows, send_reminder
from app.routes.dashboard import _activity_rows
from app.services import create_invoice
from app.validation import InvoiceProposal

SETTINGS = Settings(database_url="sqlite://", mock_webhooks=True)


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


def fail_first_commit_after_paypal_sends(session, gateway) -> None:
    """The database refuses the first commit after PayPal sent the reminder."""
    state = {"sent": False, "failed": False}
    real_send = gateway.send_reminder
    real_commit = session.commit

    def send(*args):
        real_send(*args)
        state["sent"] = True

    def commit():
        if state["sent"] and not state["failed"]:
            state["failed"] = True
            raise OperationalError("COMMIT", {}, Exception("database is locked"))
        real_commit()

    gateway.send_reminder = send
    session.commit = commit


def test_a_failed_commit_after_paypal_sends_does_not_send_twice(session):
    gateway = MockPayPal(True)
    reminder = pending_reminder(session, gateway)
    fail_first_commit_after_paypal_sends(session, gateway)

    with pytest.raises(OperationalError):
        send_reminder(session, reminder, gateway)
    session.refresh(reminder)
    assert reminder.status == "PENDING"
    assert len(gateway.sent_reminders) == 1

    outcome = send_reminder(session, reminder, gateway)
    assert len(gateway.sent_reminders) == 1, "the retry must not remind the client through PayPal again"
    assert outcome.reminder.status == "SENT"
    assert outcome.reminder.sent_channels == "paypal"
    assert any("Check PayPal" in warning for warning in outcome.warnings)


def test_a_failed_commit_after_paypal_sends_still_lets_email_retry(session):
    gateway = MockPayPal(True)
    mailer = MockMailer()
    reminder = pending_reminder(session, gateway)
    fail_first_commit_after_paypal_sends(session, gateway)

    with pytest.raises(OperationalError):
        send_reminder(session, reminder, gateway, mailer, "both")
    assert mailer.outbox == []

    outcome = send_reminder(session, reminder, gateway, mailer, "both")
    assert len(gateway.sent_reminders) == 1
    assert len(mailer.outbox) == 1
    assert outcome.reminder.sent_channels == "paypal,email"


def test_a_paypal_error_leaves_paypal_free_to_retry(session):
    gateway = MockPayPal(True)
    reminder = pending_reminder(session, gateway)
    real_send = gateway.send_reminder

    def refuse(*args):
        raise PayPalError("PayPal is down.")

    gateway.send_reminder = refuse
    with pytest.raises(PayPalError):
        send_reminder(session, reminder, gateway)
    session.refresh(reminder)
    assert reminder.status == "PENDING"
    assert reminder.paypal_attempted_at is None

    gateway.send_reminder = real_send
    outcome = send_reminder(session, reminder, gateway)
    assert len(gateway.sent_reminders) == 1
    assert outcome.warnings == []
    assert outcome.reminder.sent_channels == "paypal"


def test_email_failure_still_retries_email_only(session):
    gateway = MockPayPal(True)
    mailer = MockMailer()
    reminder = pending_reminder(session, gateway)
    mailer.fail_with = "smtp down"
    with pytest.raises(MailError):
        send_reminder(session, reminder, gateway, mailer, "both")
    mailer.fail_with = None
    outcome = send_reminder(session, reminder, gateway, mailer, "both")
    assert len(gateway.sent_reminders) == 1
    assert len(mailer.outbox) == 1
    assert outcome.warnings == []


def test_an_attempt_alone_does_not_count_as_delivered(session):
    gateway = MockPayPal(True)
    reminder = pending_reminder(session, gateway)
    reminder.paypal_attempted_at = utcnow()
    session.add(reminder)
    session.commit()

    [row] = reminder_rows(session, reminder.user_id)
    assert row["sent_channels"] == [] and row["paypal_sent"] is False
    assert all(not row["event_type"].startswith("Reminder sent") for row in _activity_rows(session, reminder.user_id))
    assert draft_reminders(session, SETTINGS, scope=reminder.user_id) == []


def test_an_older_database_gains_the_attempt_column(tmp_path):
    url = f"sqlite:///{tmp_path / 'old.db'}"
    engine = make_engine(url)
    init_db(engine)
    with engine.begin() as connection:
        connection.execute(text('ALTER TABLE "reminder" DROP COLUMN "paypal_attempted_at"'))
    init_db(create_engine(url))
    with engine.connect() as connection:
        columns = {row[1] for row in connection.execute(text('PRAGMA table_info("reminder")'))}
    assert "paypal_attempted_at" in columns
