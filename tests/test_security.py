"""Security and input-limit tests (issue #18)."""

FORM = {
    "client_name": "Acme Studio",
    "client_email": "billing@acme.test",
    "description": "Logo design",
    "amount": "1200.00",
    "currency": "USD",
    "terms_days": "14",
}


def test_security_headers_are_present(client):
    response = client.get("/healthz")
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["x-frame-options"] == "DENY"
    assert response.headers["referrer-policy"] == "no-referrer"
    assert "frame-ancestors 'none'" in response.headers["content-security-policy"]


def test_long_client_name_is_rejected(client):
    response = client.post("/invoices", data={**FORM, "client_name": "x" * 121})
    assert response.status_code == 422
    assert "120 characters" in response.text


def test_long_description_is_rejected(client):
    response = client.post("/invoices", data={**FORM, "description": "x" * 501})
    assert response.status_code == 422
    assert "500 characters" in response.text


def test_amount_over_cap_is_rejected(client):
    response = client.post("/invoices", data={**FORM, "amount": "10000.01"})
    assert response.status_code == 422
    assert "at most" in response.text


def test_unknown_currency_is_rejected(client):
    response = client.post("/invoices", data={**FORM, "currency": "EUR"})
    assert response.status_code == 422
    assert "Currency must be" in response.text


def test_long_prompt_is_rejected(client):
    response = client.post("/parse", data={"description": "x" * 2001})
    assert response.status_code == 422
    assert "2,000" in response.json()["error"]


def test_oversized_webhook_is_rejected(client):
    response = client.post(
        "/webhooks/paypal",
        content=b"x" * 1_000_001,
        headers={"content-type": "application/json"},
    )
    assert response.status_code == 413


def test_invalid_webhook_json_is_rejected(client):
    response = client.post("/webhooks/paypal", content=b"{not json")
    assert response.status_code == 400
    assert response.json()["ok"] is False


def test_webhook_without_event_id_is_rejected(client):
    response = client.post(
        "/webhooks/paypal",
        content=b'{"event_type": "INVOICING.INVOICE.PAID"}',
    )
    assert response.status_code == 400
