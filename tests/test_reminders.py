"""Reminder drafting and approval tests (issue #15)."""

from datetime import timedelta
from decimal import Decimal

import pytest
from sqlmodel import Session

from app.config import Settings
from app.models import Client, Invoice, utcnow
from app.reminders import (
    draft_reminders,
    edit_reminder,
    reject_reminder,
    reminder_text,
    send_reminder,
)
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
            session, proposal, client.app.state.settings, client.app.state.gateway, user_id=client.user_id
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


def test_scan_skips_invoices_without_a_paypal_id(client):
    invoice_id = seed_invoice(client)
    with Session(client.app.state.engine) as session:
        invoice = session.get(Invoice, invoice_id)
        invoice.paypal_invoice_id = None
        session.add(invoice)
        session.commit()
        assert draft_reminders(session, client.app.state.settings) == []


def test_approval_sends_once(client):
    seed_invoice(client)
    with Session(client.app.state.engine) as session:
        reminder = draft_reminders(session, client.app.state.settings)[0]
        send_reminder(session, reminder, client.app.state.gateway)
        assert reminder.status == "SENT"
        assert len(client.app.state.gateway.sent_reminders) == 1
        with pytest.raises(ValueError):
            send_reminder(session, reminder, client.app.state.gateway)


def test_edit_changes_the_draft_without_sending(client):
    seed_invoice(client)
    with Session(client.app.state.engine) as session:
        reminder = draft_reminders(session, client.app.state.settings)[0]
        edit_reminder(session, reminder, "A shorter, warmer nudge.")
        assert reminder.status == "PENDING"
        assert reminder.draft_text == "A shorter, warmer nudge."
        assert reminder.tone == "custom"
    assert client.app.state.gateway.sent_reminders == []


def test_edit_refuses_an_empty_draft(client):
    seed_invoice(client)
    with Session(client.app.state.engine) as session:
        reminder = draft_reminders(session, client.app.state.settings)[0]
        with pytest.raises(ValueError, match="cannot be empty"):
            edit_reminder(session, reminder, "   ")


def test_reject_keeps_paypal_quiet(client):
    seed_invoice(client)
    with Session(client.app.state.engine) as session:
        reminder = draft_reminders(session, client.app.state.settings)[0]
        reject_reminder(session, reminder)
        assert reminder.status == "REJECTED"
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


def test_http_edit_then_approve_sends_the_edited_text(client):
    seed_invoice(client)
    client.post("/api/reminders/scan")
    reminder_id = client.get("/api/reminders").json()[0]["id"]
    response = client.post(
        f"/api/reminders/{reminder_id}/edit",
        data={"draft_text": "Custom nudge from the operator."},
    )
    assert response.status_code == 200
    assert response.json()["draft_text"] == "Custom nudge from the operator."
    approve = client.post(f"/api/reminders/{reminder_id}/approve")
    assert approve.status_code == 200
    sent = client.app.state.gateway.sent_reminders[0]
    assert sent["note"] == "Custom nudge from the operator."


def test_http_edit_rejects_an_empty_draft(client):
    seed_invoice(client)
    client.post("/api/reminders/scan")
    reminder_id = client.get("/api/reminders").json()[0]["id"]
    response = client.post(f"/api/reminders/{reminder_id}/edit", data={"draft_text": "  "})
    assert response.status_code == 422


def test_http_edit_rejects_a_sent_draft(client):
    seed_invoice(client)
    client.post("/api/reminders/scan")
    reminder_id = client.get("/api/reminders").json()[0]["id"]
    client.post(f"/api/reminders/{reminder_id}/approve")
    response = client.post(
        f"/api/reminders/{reminder_id}/edit", data={"draft_text": "Too late."}
    )
    assert response.status_code == 409


def test_http_reject(client):
    seed_invoice(client)
    client.post("/api/reminders/scan")
    reminder_id = client.get("/api/reminders").json()[0]["id"]
    response = client.post(f"/api/reminders/{reminder_id}/reject")
    assert response.status_code == 200
    assert response.json()["status"] == "REJECTED"


def test_dashboard_shows_reminder_panel(client):
    seed_invoice(client)
    client.post("/api/reminders/scan")
    page = client.get("/")
    assert page.status_code == 200
    assert "Approve and send" in page.text
    assert "Save draft" in page.text
    assert "Reject draft" in page.text
    assert 'class="draft-edit"' in page.text
    assert 'id="action-error"' in page.text
