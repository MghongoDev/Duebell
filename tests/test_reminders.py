"""Reminder drafting and approval tests (issue #15)."""

from datetime import timedelta
from decimal import Decimal

import pytest
from sqlmodel import Session

from app.config import Settings
from app.models import Client, Invoice, utcnow
from app.reminders import dismiss_reminder, draft_reminders, reminder_text, send_reminder
from app.services import create_invoice
from app.validation import InvoiceProposal


def seed_invoice(client, *, due_days=-3, risk="HIGH"):
    proposal = InvoiceProposal(
        client_name="Acme Studio",
        client_email="billing@acme.test",
        description="Logo design",
        amount=Decimal("1200.00"),
        currency="USD",
        terms_days=14,
    )
    with Session(client.app.state.engine) as session:
        invoice = create_invoice(
            session, proposal, client.app.state.settings, client.app.state.gateway
        )
        invoice.due_at = utcnow() + timedelta(days=due_days)
        invoice.risk_level = risk
        session.add(invoice)
        session.commit()
        session.refresh(invoice)
        return invoice.id


def test_text_uses_only_invoice_facts():
    now = utcnow()
    invoice = Invoice(
        id=1,
        client_id=1,
        invoice_number="DUE-1",
        amount_cents=120000,
        currency="USD",
        due_at=now - timedelta(days=20),
        risk_level="HIGH",
    )
    client = Client(id=1, name="Acme Studio", email="billing@acme.test")
    text, tone = reminder_text(invoice, client, Settings(), now)
    assert tone == "firm"
    assert "Acme Studio" in text
    assert "DUE-1" in text
    assert "1,200.00 USD" in text


def test_scan_drafts_but_does_not_send(client):
    invoice_id = seed_invoice(client)
    with Session(client.app.state.engine) as session:
        created = draft_reminders(session, client.app.state.settings)
        assert len(created) == 1
        assert created[0].invoice_id == invoice_id
        assert created[0].status == "PENDING"
    assert client.app.state.gateway.sent_reminders == []


def test_scan_does_not_duplicate_a_pending_draft(client):
    seed_invoice(client)
    with Session(client.app.state.engine) as session:
        assert len(draft_reminders(session, client.app.state.settings)) == 1
        assert len(draft_reminders(session, client.app.state.settings)) == 0


def test_scan_skips_paid_invoices(client):
    invoice_id = seed_invoice(client)
    with Session(client.app.state.engine) as session:
        invoice = session.get(Invoice, invoice_id)
        invoice.status = "PAID"
        invoice.paid_at = utcnow()
        session.add(invoice)
        session.commit()
        assert draft_reminders(session, client.app.state.settings) == []


def test_scan_skips_far_future_low_risk(client):
    seed_invoice(client, due_days=30, risk="LOW")
    with Session(client.app.state.engine) as session:
        assert draft_reminders(session, client.app.state.settings) == []


def test_scan_drafts_due_soon_high_risk(client):
    seed_invoice(client, due_days=2, risk="HIGH")
    with Session(client.app.state.engine) as session:
        assert len(draft_reminders(session, client.app.state.settings)) == 1


def test_approval_sends_once(client):
    seed_invoice(client)
    with Session(client.app.state.engine) as session:
        reminder = draft_reminders(session, client.app.state.settings)[0]
        send_reminder(session, reminder, client.app.state.gateway)
        assert reminder.status == "SENT"
        assert len(client.app.state.gateway.sent_reminders) == 1
        with pytest.raises(ValueError):
            send_reminder(session, reminder, client.app.state.gateway)


def test_dismiss_keeps_paypal_quiet(client):
    seed_invoice(client)
    with Session(client.app.state.engine) as session:
        reminder = draft_reminders(session, client.app.state.settings)[0]
        dismiss_reminder(session, reminder)
        assert reminder.status == "DISMISSED"
    assert client.app.state.gateway.sent_reminders == []


def test_http_scan_and_approve(client):
    seed_invoice(client)
    scan = client.post("/api/reminders/scan")
    assert scan.status_code == 200
    assert scan.json()["created"] == 1
    rows = client.get("/api/reminders").json()
    assert rows[0]["status"] == "PENDING"
    approve = client.post(f"/api/reminders/{rows[0]['id']}/approve")
    assert approve.status_code == 200
    assert approve.json()["status"] == "SENT"
    assert len(client.app.state.gateway.sent_reminders) == 1


def test_http_dismiss(client):
    seed_invoice(client)
    client.post("/api/reminders/scan")
    reminder_id = client.get("/api/reminders").json()[0]["id"]
    response = client.post(f"/api/reminders/{reminder_id}/dismiss")
    assert response.status_code == 200
    assert response.json()["status"] == "DISMISSED"


def test_dashboard_shows_reminder_panel(client):
    seed_invoice(client)
    client.post("/api/reminders/scan")
    page = client.get("/")
    assert page.status_code == 200
    assert "Approve and send" in page.text
