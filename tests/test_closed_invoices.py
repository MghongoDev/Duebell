"""Nothing reaches a client about a paid, cancelled or refunded invoice (issue #36)."""

import pytest
from sqlmodel import Session

from app.models import Invoice
from app.services import dashboard_totals, invoice_rows
from tests.test_reminders import seed_invoice
from tests.test_webhooks import event_body


def pending_draft(client):
    invoice_id = seed_invoice(client)
    assert client.post("/api/reminders/scan").json()["created"] == 1
    [draft] = client.get("/api/reminders").json()
    return invoice_id, draft["id"]


def set_status(client, invoice_id, status):
    """Close an invoice behind the app's back, as an older build or a direct edit could."""
    with Session(client.app.state.engine) as session:
        invoice = session.get(Invoice, invoice_id)
        invoice.status = status
        session.add(invoice)
        session.commit()
        return invoice.paypal_invoice_id


def draft_status(client, draft_id):
    return next(row for row in client.get("/api/reminders").json() if row["id"] == draft_id)["status"]


@pytest.mark.parametrize("status", ["PAID", "CANCELLED", "REFUNDED"])
def test_approving_a_draft_for_a_closed_invoice_is_refused_and_rejects_it(client, status):
    invoice_id, draft_id = pending_draft(client)
    set_status(client, invoice_id, status)

    response = client.post(f"/api/reminders/{draft_id}/approve")
    assert response.status_code == 409
    assert draft_status(client, draft_id) == "REJECTED"
    assert client.app.state.gateway.sent_reminders == []


def test_a_paid_webhook_rejects_the_pending_draft(client):
    invoice_id, draft_id = pending_draft(client)
    with Session(client.app.state.engine) as session:
        paypal_id = session.get(Invoice, invoice_id).paypal_invoice_id

    client.post("/webhooks/paypal", content=event_body("WH-PAID", "INVOICING.INVOICE.PAID", paypal_id))
    assert draft_status(client, draft_id) == "REJECTED"


def test_a_scan_rejects_drafts_for_invoices_closed_since(client):
    invoice_id, draft_id = pending_draft(client)
    set_status(client, invoice_id, "CANCELLED")

    client.post("/api/reminders/scan")
    assert draft_status(client, draft_id) == "REJECTED"


def test_simulated_payment_marks_paid_and_rejects_the_draft(client):
    invoice_id, draft_id = pending_draft(client)
    assert client.post(f"/api/invoices/{invoice_id}/simulate-payment").status_code == 200
    assert draft_status(client, draft_id) == "REJECTED"


def test_simulated_payment_on_a_closed_invoice_changes_nothing(client):
    invoice_id = seed_invoice(client)
    paypal_id = set_status(client, invoice_id, "CANCELLED")

    response = client.post(f"/api/invoices/{invoice_id}/simulate-payment")
    assert response.status_code == 409
    assert client.app.state.gateway.invoices[paypal_id].get("paid_cents") is None
    with Session(client.app.state.engine) as session:
        invoice = session.get(Invoice, invoice_id)
        assert invoice.status == "CANCELLED"
        assert invoice.paid_at is None


def test_outstanding_counts_overdue_invoices(client):
    invoice_id = seed_invoice(client)
    set_status(client, invoice_id, "OVERDUE")
    with Session(client.app.state.engine) as session:
        totals = dashboard_totals(invoice_rows(session, client.user_id))
    assert totals["outstanding"] == {"USD": 120_000}
