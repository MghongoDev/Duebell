"""A webhook reads the status from the invoice it carries (issue #112).

PayPal sends INVOICING.INVOICE.PAID when an invoice is paid, partially paid,
or has a payment pending, and INVOICING.INVOICE.REFUNDED when it is refunded
or partially refunded. The invoice in the event says which.
"""

import json

import pytest
from sqlmodel import Session

from app.models import Invoice
from app.services import dashboard_totals, invoice_rows, may_replace
from tests.test_closed_invoices import draft_status, pending_draft
from tests.test_reminders import seed_invoice

PAID = "INVOICING.INVOICE.PAID"
REFUNDED = "INVOICING.INVOICE.REFUNDED"


def event(event_id, event_type, paypal_invoice_id, status=None, *, nested=True):
    invoice = {"id": paypal_invoice_id}
    if status:
        invoice["status"] = status
    resource = {"invoice": invoice} if nested else invoice
    return json.dumps({"id": event_id, "event_type": event_type, "resource": resource})


def paypal_id(client, invoice_id):
    with Session(client.app.state.engine) as session:
        return session.get(Invoice, invoice_id).paypal_invoice_id


def status_of(client, invoice_id):
    with Session(client.app.state.engine) as session:
        return session.get(Invoice, invoice_id).status


def totals(client):
    with Session(client.app.state.engine) as session:
        return dashboard_totals(invoice_rows(session, client.user_id))


@pytest.mark.parametrize("nested", [True, False])
def test_a_paid_event_for_a_partly_paid_invoice_leaves_it_open_and_owed(client, nested):
    invoice_id, draft_id = pending_draft(client)
    body = event("WH-P1", PAID, paypal_id(client, invoice_id), "PARTIALLY_PAID", nested=nested)

    response = client.post("/webhooks/paypal", content=body)
    assert response.json()["applied"] is True
    assert response.json()["status"] == "PARTIALLY_PAID"
    assert status_of(client, invoice_id) == "PARTIALLY_PAID"
    assert draft_status(client, draft_id) == "PENDING"
    assert totals(client)["outstanding"] == {"USD": 120000}
    assert totals(client)["paid"] == {}
    row = client.get("/api/invoices").json()[0]
    assert row["closed"] is False
    assert row["paid_at"] is None


def test_a_partly_paid_invoice_is_still_remindable(client):
    invoice_id = seed_invoice(client)
    client.post("/webhooks/paypal", content=event("WH-P2", PAID, paypal_id(client, invoice_id), "PARTIALLY_PAID"))
    assert client.post("/api/reminders/scan").json()["created"] == 1


def test_a_paid_event_for_a_fully_paid_invoice_still_closes_it(client):
    invoice_id, draft_id = pending_draft(client)
    client.post("/webhooks/paypal", content=event("WH-P3", PAID, paypal_id(client, invoice_id), "PAID"))
    assert status_of(client, invoice_id) == "PAID"
    assert draft_status(client, draft_id) == "REJECTED"
    assert totals(client)["outstanding"] == {}


def test_an_event_without_a_status_falls_back_to_its_type(client):
    invoice_id = seed_invoice(client)
    client.post("/webhooks/paypal", content=event("WH-P4", PAID, paypal_id(client, invoice_id)))
    assert status_of(client, invoice_id) == "PAID"


def test_a_partial_refund_keeps_the_invoice_open_then_a_full_refund_closes_it(client):
    invoice_id = seed_invoice(client)
    pid = paypal_id(client, invoice_id)
    client.post("/webhooks/paypal", content=event("WH-R1", PAID, pid, "PAID"))
    client.post("/webhooks/paypal", content=event("WH-R2", REFUNDED, pid, "PARTIALLY_REFUNDED"))
    assert status_of(client, invoice_id) == "PARTIALLY_REFUNDED"
    assert client.get("/api/invoices").json()[0]["closed"] is False

    client.post("/webhooks/paypal", content=event("WH-R3", REFUNDED, pid, "REFUNDED"))
    assert status_of(client, invoice_id) == "REFUNDED"


def test_refresh_moves_a_partly_paid_invoice_on_to_paid(client):
    invoice_id = seed_invoice(client)
    pid = paypal_id(client, invoice_id)
    client.app.state.gateway.invoices[pid]["status"] = "PARTIALLY_PAID"
    assert client.post(f"/api/invoices/{invoice_id}/refresh").json()["status"] == "PARTIALLY_PAID"
    client.app.state.gateway.invoices[pid]["status"] = "PAID"
    assert client.post(f"/api/invoices/{invoice_id}/refresh").json()["status"] == "PAID"


@pytest.mark.parametrize(
    "current, new, allowed",
    [
        ("SENT", "PAYMENT_PENDING", True),
        ("SENT", "PARTIALLY_PAID", True),
        ("PAYMENT_PENDING", "PARTIALLY_PAID", True),
        ("PAYMENT_PENDING", "SENT", True),
        ("PARTIALLY_PAID", "PAYMENT_PENDING", True),
        ("PARTIALLY_PAID", "PAID", True),
        ("PARTIALLY_PAID", "PARTIALLY_REFUNDED", True),
        ("PARTIALLY_PAID", "SENT", False),
        ("PARTIALLY_PAID", "OVERDUE", False),
        ("PAID", "PARTIALLY_PAID", False),
        ("PAID", "PAYMENT_PENDING", False),
        ("PAID", "PARTIALLY_REFUNDED", True),
        ("MARKED_AS_PAID", "PARTIALLY_REFUNDED", True),
        ("PARTIALLY_REFUNDED", "REFUNDED", True),
        ("PARTIALLY_REFUNDED", "PAID", False),
        ("PARTIALLY_REFUNDED", "SENT", False),
        ("REFUNDED", "PARTIALLY_REFUNDED", False),
        ("CANCELLED", "PARTIALLY_PAID", False),
    ],
)
def test_partial_statuses_move_forward_only(current, new, allowed):
    assert may_replace(current, new) is allowed


# PayPal's other closed statuses are stored as PayPal names them, like
# MARKED_AS_PAID (issue #112).
CLOSED_BY_PAYPAL = [
    (PAID, "PAID_EXTERNAL", True),
    (REFUNDED, "MARKED_AS_REFUNDED", False),
    (REFUNDED, "REFUNDED_EXTERNAL", False),
    ("INVOICING.INVOICE.CANCELLED", "AUTO_CANCELLED", False),
]


def assert_closed(client, invoice_id, draft_id, status, paid):
    assert status_of(client, invoice_id) == status
    assert draft_status(client, draft_id) == "REJECTED"
    row = client.get("/api/invoices").json()[0]
    assert row["closed"] is True
    assert row["overdue"] is False
    assert (row["paid_at"] is not None) is paid
    assert totals(client)["outstanding"] == {}
    assert totals(client)["paid"] == ({"USD": 120000} if paid else {})
    assert client.post("/api/reminders/scan").json()["created"] == 0


@pytest.mark.parametrize("event_type, status, paid", CLOSED_BY_PAYPAL)
def test_a_webhook_with_a_closed_paypal_status_closes_the_invoice(client, event_type, status, paid):
    invoice_id, draft_id = pending_draft(client)
    response = client.post("/webhooks/paypal", content=event("WH-C1", event_type, paypal_id(client, invoice_id), status))
    assert response.json()["applied"] is True
    assert_closed(client, invoice_id, draft_id, status, paid)


@pytest.mark.parametrize("event_type, status, paid", CLOSED_BY_PAYPAL)
def test_a_refresh_that_finds_a_closed_paypal_status_closes_the_invoice(client, event_type, status, paid):
    invoice_id, draft_id = pending_draft(client)
    client.app.state.gateway.invoices[paypal_id(client, invoice_id)]["status"] = status
    assert client.post(f"/api/invoices/{invoice_id}/refresh").json()["status"] == status
    assert_closed(client, invoice_id, draft_id, status, paid)


@pytest.mark.parametrize("status", ["UNPAID", "SCHEDULED", "SHARED"])
def test_paypal_statuses_duebell_does_not_track_change_nothing(client, status):
    invoice_id = seed_invoice(client)
    pid = paypal_id(client, invoice_id)
    client.post("/webhooks/paypal", content=event("WH-U1", PAID, pid, status))
    assert status_of(client, invoice_id) == "SENT"
    client.app.state.gateway.invoices[pid]["status"] = status
    assert client.post(f"/api/invoices/{invoice_id}/refresh").json()["status"] == "SENT"


@pytest.mark.parametrize(
    "current, new, allowed",
    [
        ("PAID_EXTERNAL", "REFUNDED_EXTERNAL", True),
        ("PAID_EXTERNAL", "MARKED_AS_REFUNDED", True),
        ("PAID_EXTERNAL", "PARTIALLY_REFUNDED", True),
        ("PAID", "MARKED_AS_REFUNDED", True),
        ("PARTIALLY_REFUNDED", "REFUNDED_EXTERNAL", True),
        ("PAID_EXTERNAL", "SENT", False),
        ("AUTO_CANCELLED", "SENT", False),
        ("AUTO_CANCELLED", "PAID", False),
        ("MARKED_AS_REFUNDED", "PAID", False),
        ("REFUNDED_EXTERNAL", "PARTIALLY_REFUNDED", False),
        ("SENT", "AUTO_CANCELLED", True),
    ],
)
def test_closed_paypal_statuses_move_forward_only(current, new, allowed):
    assert may_replace(current, new) is allowed
