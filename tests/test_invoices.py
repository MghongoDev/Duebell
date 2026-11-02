"""HTTP seam tests for invoice creation and listing (issue #9)."""

FORM = {
    "client_name": "Acme Studio",
    "client_email": "billing@acme.test",
    "description": "Logo design",
    "amount": "1200.00",
    "currency": "USD",
    "terms_days": "14",
}


def test_create_and_list_invoice(client):
    response = client.post("/invoices", data=FORM, follow_redirects=True)
    assert response.status_code == 200
    assert "Acme Studio" in response.text
    assert "DUE-" in response.text
    assert "1,200.00" in response.text


def test_api_lists_created_invoice(client):
    client.post("/invoices", data=FORM, follow_redirects=False)
    rows = client.get("/api/invoices").json()
    assert len(rows) == 1
    assert rows[0]["client_name"] == "Acme Studio"
    assert rows[0]["amount_cents"] == 120000
    assert rows[0]["status"] == "DRAFT"


def test_bad_email_returns_field_error(client):
    data = dict(FORM, client_email="not-an-email")
    response = client.post("/invoices", data=data)
    assert response.status_code == 422
    assert "valid email" in response.text


def test_amount_above_cap_is_rejected(client):
    data = dict(FORM, amount="999999.00")
    response = client.post("/invoices", data=data)
    assert response.status_code == 422
    assert "at most" in response.text
