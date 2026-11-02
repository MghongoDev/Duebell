"""Sandbox adapter payload tests that stay offline (issues #10, #18)."""

from datetime import datetime, timezone

from app.config import Settings
from app.paypal import SandboxPayPal, term_type

NOW = datetime(2026, 11, 3, 12, 0, tzinfo=timezone.utc)


class FakeResponse:
    def __init__(self, data=None, headers=None, status_code=200, text=""):
        self._data = data or {}
        self.headers = headers or {}
        self.status_code = status_code
        self.text = text

    def json(self):
        return self._data


class FakeClient:
    def __init__(self):
        self.requests = []
        self.last_json = None

    def post(self, url, **kwargs):
        self.requests.append(("POST", url, kwargs))
        return FakeResponse({"access_token": "token", "expires_in": 3000})

    def request(self, method, url, **kwargs):
        self.requests.append((method, url, kwargs))
        self.last_json = kwargs.get("json")
        return FakeResponse(
            headers={"Location": "https://api.test/v2/invoicing/invoices/INV-1"}
        )


def settings(**overrides):
    values = {
        "paypal_mode": "sandbox",
        "paypal_client_id": "client-id",
        "paypal_client_secret": "client-secret",
        "paypal_invoicer_email": "billing@duebell.test",
        "paypal_base_url": "https://api.test",
    }
    values.update(overrides)
    return Settings(**values)


def test_term_mapping_uses_paypal_values():
    assert term_type(0) == "DUE_ON_RECEIPT"
    assert term_type(7) == "NET_10"
    assert term_type(14) == "NET_15"
    assert term_type(30) == "NET_30"
    assert term_type(90) == "NET_90"


def test_create_invoice_sends_a_valid_payload():
    fake = FakeClient()
    gateway = SandboxPayPal(settings(), client=fake)
    paypal_id = gateway.create_invoice(
        invoice_number="DUE-1",
        amount_cents=120000,
        currency="USD",
        description="Logo design",
        terms_days=14,
        client_name="Acme Studio",
        client_email="billing@acme.test",
        issued_at=NOW,
        due_at=NOW,
    )
    assert paypal_id == "INV-1"
    body = fake.last_json
    assert body["detail"]["invoice_number"] == "DUE-1"
    assert body["detail"]["payment_term"]["term_type"] == "NET_15"
    assert body["items"][0]["unit_amount"] == {"currency_code": "USD", "value": "1200.00"}
    assert body["primary_recipients"][0]["billing_info"]["email_address"] == "billing@acme.test"


def test_webhook_check_refuses_without_a_webhook_id():
    gateway = SandboxPayPal(settings(paypal_webhook_id=""), client=FakeClient())
    assert gateway.verify_webhook({}, "{}") is False
