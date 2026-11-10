"""A scheduled item and the invoice it is linked to stay in step (issue #79)."""

import pytest
from sqlalchemy.exc import OperationalError
from sqlmodel import Session, select

from app.models import Invoice, Schedule, ScheduledInvoice
from app.paypal import PayPalError
from app.schedules import cancel_schedule
from tests.test_approve_once import _raise
from tests.test_schedules import items, move_due, plan_form
from tests.test_webhooks import send_event


def _schedule(client) -> Schedule:
    with Session(client.app.state.engine) as session:
        return session.exec(select(Schedule)).one()


def _invoice(client, invoice_id) -> Invoice:
    with Session(client.app.state.engine) as session:
        return session.get(Invoice, invoice_id)


def _last_instalment_with_unsent_draft(client):
    """A two-part plan: the first part sent, the second linked to a draft PayPal holds unsent."""
    client.post("/invoices", data=plan_form(client, instalments="2"), follow_redirects=False)
    first, last = items(client)
    assert client.post(f"/api/scheduled/{first.id}/approve").status_code == 200
    move_due(client, last.id)
    client.post("/api/reminders/scan")

    gateway = client.app.state.gateway
    real_send = gateway.send_invoice
    gateway.send_invoice = _raise(PayPalError("PayPal is down."))
    assert client.post(f"/api/scheduled/{last.id}/approve").status_code == 502
    gateway.send_invoice = real_send

    item = items(client)[1]
    draft = _invoice(client, item.invoice_id)
    assert item.status == "PENDING"
    assert draft.status == "DRAFT" and draft.paypal_invoice_id
    return item, draft


@pytest.mark.parametrize("event_type", ["INVOICING.INVOICE.CANCELLED", "INVOICING.INVOICE.REFUNDED"])
def test_approving_after_the_linked_draft_closed_unsent_bills_a_new_invoice(client, event_type):
    item, draft = _last_instalment_with_unsent_draft(client)
    send_event(client, "WH-79-1", event_type, draft.paypal_invoice_id)
    assert _invoice(client, draft.id).status != "DRAFT"

    response = client.post(f"/api/scheduled/{item.id}/approve")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "SENT"
    assert body["invoice_id"] != draft.id, "the closed draft is unlinked and a new invoice made"
    fresh = _invoice(client, body["invoice_id"])
    assert fresh.status == "SENT" and fresh.paypal_invoice_id != draft.paypal_invoice_id
    assert fresh.amount_cents == item.amount_cents and fresh.description == item.description
    assert client.app.state.gateway.invoices[fresh.paypal_invoice_id]["status"] == "SENT"
    assert items(client)[1].invoice_id == fresh.id
    assert _invoice(client, draft.id).status != "SENT", "the closed draft is left as it was"
    assert _schedule(client).status == "DONE", "the plan finishes only once its last part is billed"


def test_sending_a_linked_draft_from_its_row_marks_the_item_sent(client):
    item, draft = _last_instalment_with_unsent_draft(client)

    response = client.post(f"/api/invoices/{draft.id}/send")

    assert response.json() == {"ok": True, "status": "SENT"}
    after = items(client)[1]
    assert after.status == "SENT" and after.invoice_id == draft.id
    assert _schedule(client).status == "DONE"
    assert client.post(f"/api/scheduled/{item.id}/reject").status_code == 409
    assert client.post(f"/api/scheduled/{item.id}/approve").status_code == 409
    assert len(client.app.state.gateway.invoices) == 2, "no second invoice for the instalment"


@pytest.mark.parametrize("event_type", ["INVOICING.INVOICE.SENT", "INVOICING.INVOICE.PAID"])
def test_a_webhook_that_sends_the_linked_draft_marks_the_item_sent(client, event_type):
    item, draft = _last_instalment_with_unsent_draft(client)

    assert send_event(client, "WH-79-2", event_type, draft.paypal_invoice_id).json()["applied"] is True

    assert items(client)[1].status == "SENT"
    assert _schedule(client).status == "DONE"


def test_a_refresh_that_finds_the_linked_draft_sent_marks_the_item_sent(client):
    item, draft = _last_instalment_with_unsent_draft(client)
    client.app.state.gateway.invoices[draft.paypal_invoice_id]["status"] = "SENT"

    assert client.post(f"/api/invoices/{draft.id}/refresh").json() == {"ok": True, "status": "SENT"}

    assert items(client)[1].status == "SENT"
    assert _schedule(client).status == "DONE"


def test_skip_is_refused_while_paypal_holds_the_linked_draft(client):
    item, draft = _last_instalment_with_unsent_draft(client)

    response = client.post(f"/api/scheduled/{item.id}/reject")

    assert response.status_code == 409
    error = response.json()["error"]
    assert "Send" in error and "cancel" in error
    assert items(client)[1].status == "PENDING"
    assert _invoice(client, draft.id).status == "DRAFT"
    assert _schedule(client).status == "ACTIVE"
    assert client.app.state.gateway.invoices[draft.paypal_invoice_id]["status"] == "DRAFT"

    # Once PayPal has cancelled the draft, the item can be skipped.
    send_event(client, "WH-79-3", "INVOICING.INVOICE.CANCELLED", draft.paypal_invoice_id)
    assert client.post(f"/api/scheduled/{item.id}/reject").json() == {"ok": True, "status": "REJECTED"}
    assert _schedule(client).status == "DONE"


def test_sending_the_draft_of_a_cancelled_schedule_records_it_as_sent(client):
    item, draft = _last_instalment_with_unsent_draft(client)
    with Session(client.app.state.engine) as session:
        cancel_schedule(session, session.get(Schedule, item.schedule_id))
    assert items(client)[1].status == "CANCELLED"

    assert client.post(f"/api/invoices/{draft.id}/send").json()["status"] == "SENT"

    assert items(client)[1].status == "SENT", "the client got it, so the item says so"
    assert _schedule(client).status == "CANCELLED"


def test_skip_is_refused_once_paypal_has_sent_the_linked_invoice(client, monkeypatch):
    client.post("/invoices", data=plan_form(client, instalments="2"), follow_redirects=False)
    item = items(client)[0]

    def send_then_fail(session, invoice, gateway, **kwargs):
        gateway.send_invoice(invoice.paypal_invoice_id)
        raise OperationalError("COMMIT", {}, Exception("database is locked"))

    monkeypatch.setattr("app.services.send_unsent_invoice", send_then_fail)
    with pytest.raises(OperationalError):
        client.post(f"/api/scheduled/{item.id}/approve")
    monkeypatch.undo()
    linked = _invoice(client, items(client)[0].invoice_id)
    send_event(client, "WH-79-4", "INVOICING.INVOICE.SENT", linked.paypal_invoice_id)
    assert items(client)[0].status == "SENT", "the webhook records the send"

    with Session(client.app.state.engine) as session:
        stale = session.get(ScheduledInvoice, item.id)
        stale.status = "PENDING"
        session.add(stale)
        session.commit()
    response = client.post(f"/api/scheduled/{item.id}/reject")
    assert response.status_code == 409
    assert "already sent" in response.json()["error"]
    assert client.post(f"/api/scheduled/{item.id}/approve").json()["status"] == "SENT"
