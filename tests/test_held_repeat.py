"""A held repeat starts whenever its draft leaves DRAFT for a sent status (issue #78)."""

import pytest
from sqlmodel import Session, select

from app.models import Invoice, Schedule, as_utc
from app.services import handle_webhook
from tests.test_retry_send import _failed_send
from tests.test_schedules import items
from tests.test_webhooks import send_event


def _schedules(client) -> list[Schedule]:
    with Session(client.app.state.engine) as session:
        return list(session.exec(select(Schedule)).all())


def _invoice(client, invoice_id) -> Invoice:
    with Session(client.app.state.engine) as session:
        return session.get(Invoice, invoice_id)


def _assert_repeat_started_once(client, row, unit="MONTH"):
    [schedule] = _schedules(client)
    invoice = _invoice(client, row["id"])
    assert schedule.interval_unit == unit
    assert schedule.user_id == client.user_id
    assert as_utc(schedule.starts_on) == as_utc(invoice.issued_at), "dates count from the invoice (issue #43)"
    assert invoice.repeat_unit is None and invoice.repeat_cycles is None, "the hold is used once"
    assert len(items(client)) == 1


@pytest.mark.parametrize("event_type", ["INVOICING.INVOICE.SENT", "INVOICING.INVOICE.PAID"])
def test_a_webhook_that_moves_the_draft_on_starts_its_held_repeat(client, event_type):
    row = _failed_send(client, repeat="MONTH", repeat_cycles="2")
    response = send_event(client, "WH-78-1", event_type, row["paypal_invoice_id"])
    assert response.json()["applied"] is True
    _assert_repeat_started_once(client, row)
    assert _schedules(client)[0].cycles_left == 1

    # The Send button now refuses, and nothing adds a second schedule.
    assert client.post(f"/api/invoices/{row['id']}/send").status_code == 409
    send_event(client, "WH-78-2", "INVOICING.INVOICE.PAID", row["paypal_invoice_id"])
    _assert_repeat_started_once(client, row)


def test_a_refresh_that_finds_the_draft_sent_starts_its_held_repeat(client):
    row = _failed_send(client, repeat="WEEK")
    client.app.state.gateway.invoices[row["paypal_invoice_id"]]["status"] = "SENT"
    response = client.post(f"/api/invoices/{row['id']}/refresh")
    assert response.json() == {"ok": True, "status": "SENT"}
    _assert_repeat_started_once(client, row, unit="WEEK")
    client.post(f"/api/invoices/{row['id']}/refresh")
    _assert_repeat_started_once(client, row, unit="WEEK")


def test_a_cancelled_draft_drops_its_held_repeat(client):
    row = _failed_send(client, repeat="MONTH")
    send_event(client, "WH-78-3", "INVOICING.INVOICE.CANCELLED", row["paypal_invoice_id"])
    assert _invoice(client, row["id"]).repeat_unit is None
    assert _schedules(client) == []
    assert client.post(f"/api/invoices/{row['id']}/send").status_code == 409
    assert _schedules(client) == []


def test_send_and_a_webhook_at_once_start_one_schedule(client):
    row = _failed_send(client, repeat="MONTH")
    gateway = client.app.state.gateway
    real_send = gateway.send_invoice

    def send_then_webhook(paypal_id):
        # PayPal sends, and its webhook lands before the Send route goes on.
        real_send(paypal_id)
        body = (
            '{"id": "WH-78-4", "event_type": "INVOICING.INVOICE.SENT", '
            f'"resource": {{"id": "{paypal_id}"}}}}'
        )
        with Session(client.app.state.engine) as other:
            assert handle_webhook(other, gateway, {}, body)["applied"] is True

    gateway.send_invoice = send_then_webhook
    response = client.post(f"/api/invoices/{row['id']}/send")
    assert response.status_code == 200, response.text
    _assert_repeat_started_once(client, row)
