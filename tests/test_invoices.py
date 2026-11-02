"""HTTP seam tests for invoice creation and listing (issues #9, #10)."""

from app.paypal import PayPalError

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
    assert rows[0]["status"] == "SENT"
    assert rows[0]["paypal_invoice_id"]


def test_create_sends_through_gateway(client):
    client.post("/invoices", data=FORM, follow_redirects=False)
    gateway = client.app.state.gateway
    assert len(gateway.invoices) == 1
    assert next(iter(gateway.invoices.values()))["status"] == "SENT"


def test_paypal_failure_is_readable(settings):
    from fastapi.testclient import TestClient
    from app.models import make_engine
    from app.web import create_app

    class FailingGateway:
        def create_invoice(self, **kwargs):
            raise PayPalError("PayPal rejected the call: sandbox said no.")

    app = create_app(settings=settings, engine=make_engine(settings.database_url), gateway=FailingGateway())
    response = TestClient(app).post("/invoices", data=FORM)
    assert response.status_code == 502
    assert "sandbox said no" in response.text


def test_new_client_gets_limited_risk(client):
    client.post("/invoices", data=FORM, follow_redirects=False)
    row = client.get("/api/invoices").json()[0]
    assert row["risk_level"] in {"MEDIUM", "HIGH"}
    assert any("New client" in reason for reason in row["risk_reasons"])


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
