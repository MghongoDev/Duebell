"""Email as a second reminder channel (issue #26)."""

from datetime import timedelta
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlmodel import Session

from app.mailer import MockMailer
from app.models import Invoice, Reminder, init_db, make_engine, utcnow
from app.reminders import draft_reminders, parse_channels
from app.services import create_invoice
from app.validation import InvoiceProposal
from app.web import create_app
from tests.conftest import sign_in


@pytest.fixture
def client(settings):
    mailer = MockMailer()
    app = create_app(settings=settings, engine=make_engine(settings.database_url), mailer=mailer)
    return sign_in(TestClient(app))


def pending_reminder(client) -> int:
    proposal = InvoiceProposal(
        client_name="Acme Studio",
        client_email="billing@acme.test",
        description="Logo design",
        amount=Decimal("1200.00"),
        currency="USD",
        terms_days=14,
    )
    state = client.app.state
    with Session(state.engine) as session:
        invoice = create_invoice(session, proposal, state.settings, state.gateway, user_id=client.user_id)
        invoice.due_at = utcnow() - timedelta(days=3)
        session.add(invoice)
        session.commit()
        return draft_reminders(session, state.settings)[0].id


def approve(client, reminder_id, channel=None):
    data = {"channel": channel} if channel else {}
    return client.post(f"/api/reminders/{reminder_id}/approve", data=data)


def test_default_channel_is_paypal(client):
    reminder_id = pending_reminder(client)
    response = approve(client, reminder_id)
    assert response.json() == {"ok": True, "status": "SENT", "sent_channels": ["paypal"]}
    assert len(client.app.state.gateway.sent_reminders) == 1
    assert client.app.state.mailer.outbox == []


def test_email_only(client):
    reminder_id = pending_reminder(client)
    response = approve(client, reminder_id, "email")
    assert response.json()["sent_channels"] == ["email"]
    assert client.app.state.gateway.sent_reminders == []
    [mail] = client.app.state.mailer.outbox
    assert mail["to"] == "billing@acme.test"
    assert mail["subject"].startswith("Reminder: invoice DUE-")
    assert "1,200.00" in mail["body"]


def test_both_channels(client):
    reminder_id = pending_reminder(client)
    response = approve(client, reminder_id, "both")
    assert response.json()["sent_channels"] == ["paypal", "email"]
    assert len(client.app.state.gateway.sent_reminders) == 1
    assert len(client.app.state.mailer.outbox) == 1


def test_failed_email_keeps_draft_pending_and_retry_skips_paypal(client):
    reminder_id = pending_reminder(client)
    mailer = client.app.state.mailer
    mailer.fail_with = "The email could not be sent: SMTPServerDisconnected. Try again."

    failed = approve(client, reminder_id, "both")
    assert failed.status_code == 502
    assert "could not be sent" in failed.json()["error"]
    assert failed.json()["sent_channels"] == ["paypal"]
    with Session(client.app.state.engine) as session:
        assert session.get(Reminder, reminder_id).status == "PENDING"

    page = client.get("/").text
    assert "Already sent via PayPal" in page

    mailer.fail_with = None
    retried = approve(client, reminder_id, "both")
    assert retried.json() == {"ok": True, "status": "SENT", "sent_channels": ["paypal", "email"]}
    assert len(client.app.state.gateway.sent_reminders) == 1
    assert len(mailer.outbox) == 1


def test_unknown_channel_is_rejected(client):
    reminder_id = pending_reminder(client)
    response = approve(client, reminder_id, "sms")
    assert response.status_code == 422
    assert client.app.state.gateway.sent_reminders == []


def test_activity_feed_names_the_channel(client):
    reminder_id = pending_reminder(client)
    approve(client, reminder_id, "both")
    page = client.get("/").text
    assert "Reminder sent via PayPal and email" in page


def test_email_works_for_an_invoice_without_paypal_id(client):
    reminder_id = pending_reminder(client)
    with Session(client.app.state.engine) as session:
        reminder = session.get(Reminder, reminder_id)
        invoice = session.get(Invoice, reminder.invoice_id)
        invoice.paypal_invoice_id = None
        session.add(invoice)
        session.commit()
    assert approve(client, reminder_id, "paypal").status_code == 409
    assert approve(client, reminder_id, "email").json()["status"] == "SENT"


def test_parse_channels():
    assert parse_channels(None) == ("paypal",)
    assert parse_channels("both") == ("paypal", "email")
    assert parse_channels(["email", "paypal"]) == ("paypal", "email")
    with pytest.raises(ValueError):
        parse_channels("fax")


def test_old_database_gains_the_new_column(tmp_path):
    url = f"sqlite:///{tmp_path / 'old.db'}"
    old = create_engine(url)
    with old.begin() as connection:
        connection.execute(
            text(
                "CREATE TABLE reminder (id INTEGER PRIMARY KEY, invoice_id INTEGER, draft_text TEXT, "
                "tone TEXT, status TEXT, created_at DATETIME, sent_at DATETIME)"
            )
        )
        connection.execute(
            text("INSERT INTO reminder (invoice_id, draft_text, tone, status) VALUES (1, 'hi', 'friendly', 'SENT')")
        )
    old.dispose()

    engine = make_engine(url)
    init_db(engine)
    with Session(engine) as session:
        assert session.get(Reminder, 1).sent_channels == ""
