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
