"""HTTP seam tests for payment sync (issue #11)."""

import json

FORM = {
    "client_name": "Acme Studio",
    "client_email": "billing@acme.test",
    "description": "Logo design",
    "amount": "1200.00",
    "currency": "USD",
    "terms_days": "14",
}


def create(client):
    client.post("/invoices", data=FORM, follow_redirects=False)
    return client.get("/api/invoices").json()[0]


def event_body(event_id, event_type, paypal_invoice_id):
    return json.dumps(
        {
            "id": event_id,
            "event_type": event_type,
            "resource": {"id": paypal_invoice_id},
        }
    )


def test_paid_webhook_updates_invoice(client):
    row = create(client)
    response = client.post(
        "/webhooks/paypal",
        content=event_body("WH-1", "INVOICING.INVOICE.PAID", row["paypal_invoice_id"]),
    )
    assert response.status_code == 200
    assert response.json()["applied"] is True
    assert client.get("/api/invoices").json()[0]["status"] == "PAID"


def test_duplicate_webhook_is_ignored(client):
    row = create(client)
    body = event_body("WH-2", "INVOICING.INVOICE.PAID", row["paypal_invoice_id"])
    first = client.post("/webhooks/paypal", content=body)
    second = client.post("/webhooks/paypal", content=body)
    assert first.status_code == 200
    assert second.status_code == 200
    assert second.json()["duplicate"] is True
    assert client.get("/api/invoices").json()[0]["status"] == "PAID"


def test_bad_signature_is_rejected(client):
    row = create(client)
    client.app.state.gateway.verify_result = False
    response = client.post(
        "/webhooks/paypal",
        content=event_body("WH-3", "INVOICING.INVOICE.PAID", row["paypal_invoice_id"]),
    )
    assert response.status_code == 400
    assert client.get("/api/invoices").json()[0]["status"] == "SENT"


def test_refresh_reads_paypal_status(client):
    row = create(client)
    client.app.state.gateway.invoices[row["paypal_invoice_id"]]["status"] = "PAID"
    response = client.post(f"/api/invoices/{row['id']}/refresh")
    assert response.status_code == 200
    assert response.json()["status"] == "PAID"
    assert client.get("/api/invoices").json()[0]["status"] == "PAID"


def test_simulate_payment_marks_paid(client):
    row = create(client)
    response = client.post(f"/api/invoices/{row['id']}/simulate-payment")
    assert response.status_code == 200
    assert response.json()["status"] == "PAID"
    assert client.get("/api/invoices").json()[0]["status"] == "PAID"


def test_unknown_invoice_returns_404(client):
    assert client.post("/api/invoices/999/refresh").status_code == 404


def first_row(client):
    return client.get("/api/invoices").json()[0]


def send_event(client, event_id, event_type, paypal_invoice_id):
    return client.post(
        "/webhooks/paypal",
        content=event_body(event_id, event_type, paypal_invoice_id),
    )


def test_sent_after_paid_does_not_reopen_invoice(client):
    # PayPal can redeliver SENT under a new event ID after PAID (issue #37).
    row = create(client)
    send_event(client, "WH-10", "INVOICING.INVOICE.PAID", row["paypal_invoice_id"])
    paid_at = first_row(client)["paid_at"]
    response = send_event(client, "WH-11", "INVOICING.INVOICE.SENT", row["paypal_invoice_id"])
    assert response.status_code == 200
    assert response.json()["applied"] is False
    after = first_row(client)
    assert after["status"] == "PAID"
    assert after["paid_at"] == paid_at


def test_sent_after_cancelled_does_not_reopen_invoice(client):
    row = create(client)
    send_event(client, "WH-12", "INVOICING.INVOICE.CANCELLED", row["paypal_invoice_id"])
    response = send_event(client, "WH-13", "INVOICING.INVOICE.SENT", row["paypal_invoice_id"])
    assert response.status_code == 200
    assert response.json()["applied"] is False
    assert first_row(client)["status"] == "CANCELLED"


def test_refused_update_is_still_stored_as_event(client):
    row = create(client)
    send_event(client, "WH-14", "INVOICING.INVOICE.PAID", row["paypal_invoice_id"])
    send_event(client, "WH-15", "INVOICING.INVOICE.SENT", row["paypal_invoice_id"])
    again = send_event(client, "WH-15", "INVOICING.INVOICE.SENT", row["paypal_invoice_id"])
    assert again.json()["duplicate"] is True


def test_refund_after_paid_is_applied(client):
    row = create(client)
    send_event(client, "WH-16", "INVOICING.INVOICE.PAID", row["paypal_invoice_id"])
    response = send_event(client, "WH-17", "INVOICING.INVOICE.REFUNDED", row["paypal_invoice_id"])
    assert response.json()["applied"] is True
    assert first_row(client)["status"] == "REFUNDED"


def test_refresh_does_not_move_paid_back_to_sent(client):
    row = create(client)
    send_event(client, "WH-18", "INVOICING.INVOICE.PAID", row["paypal_invoice_id"])
    client.app.state.gateway.invoices[row["paypal_invoice_id"]]["status"] = "SENT"
    response = client.post(f"/api/invoices/{row['id']}/refresh")
    assert response.json()["status"] == "PAID"


def test_refresh_does_not_move_sent_back_to_draft(client):
    row = create(client)
    client.app.state.gateway.invoices[row["paypal_invoice_id"]]["status"] = "DRAFT"
    response = client.post(f"/api/invoices/{row['id']}/refresh")
    assert response.json()["status"] == "SENT"


def test_refresh_ignores_unknown_status(client):
    row = create(client)
    client.app.state.gateway.invoices[row["paypal_invoice_id"]]["status"] = "UNKNOWN"
    response = client.post(f"/api/invoices/{row['id']}/refresh")
    assert response.json()["status"] == "SENT"
    assert first_row(client)["status"] == "SENT"


def test_non_utf8_body_is_rejected(client):
    create(client)
    response = client.post("/webhooks/paypal", content=b"\xff\xfe{not utf-8}")
    assert response.status_code == 400
    assert response.json()["ok"] is False
