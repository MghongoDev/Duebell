"""Sandbox adapter payload tests that stay offline (issues #10, #18)."""

from datetime import datetime, timezone

import httpx

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


def test_create_invoice_retries_a_transport_error():
    class FlakyClient(FakeClient):
        def __init__(self):
            super().__init__()
            self.failures = 1

        def request(self, method, url, **kwargs):
            if self.failures:
                self.failures -= 1
                raise httpx.ConnectError("network down")
            return super().request(method, url, **kwargs)

    fake = FlakyClient()
    gateway = SandboxPayPal(settings(), client=fake, retry_delay=0)
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
    invoice_calls = [item for item in fake.requests if item[1] == "/v2/invoicing/invoices"]
    assert len(invoice_calls) == 1
    assert fake.failures == 0


def test_webhook_check_refuses_without_a_webhook_id():
    gateway = SandboxPayPal(settings(paypal_webhook_id=""), client=FakeClient())
    assert gateway.verify_webhook({}, "{}") is False


class RecordingFlakyClient(FakeClient):
    """Fails the first `failures` requests with a 503, recording every attempt's headers."""

    def __init__(self, failures=0):
        super().__init__()
        self.failures = failures
        self.attempts = []

    def request(self, method, url, **kwargs):
        self.attempts.append((method, url, dict(kwargs.get("headers") or {})))
        if self.failures:
            self.failures -= 1
            return FakeResponse(status_code=503, text="busy")
        return super().request(method, url, **kwargs)


def test_a_retried_reminder_reuses_its_request_id():
    fake = RecordingFlakyClient(failures=2)
    gateway = SandboxPayPal(settings(), client=fake, retry_delay=0)
    gateway.send_reminder("INV-1", "Reminder: invoice DUE-1", "Please pay.")
    ids = [headers.get("PayPal-Request-Id") for _, url, headers in fake.attempts if url.endswith("/remind")]
    assert len(ids) == 3
    assert ids[0] and len(set(ids)) == 1, "every retry of one action must carry the same ID"
    assert len(ids[0]) <= 38


def test_separate_actions_get_separate_request_ids():
    fake = RecordingFlakyClient()
    gateway = SandboxPayPal(settings(), client=fake, retry_delay=0)
    gateway.send_invoice("INV-1")
    gateway.send_reminder("INV-1", "Reminder", "Please pay.")
    gateway.send_reminder("INV-1", "Reminder", "Please pay again.")
    gateway.record_payment("INV-1", 120000, "USD")
    ids = [headers.get("PayPal-Request-Id") for method, _, headers in fake.attempts if method == "POST"]
    assert len(ids) == 4 and len(set(ids)) == 4


def test_reads_carry_no_request_id():
    fake = RecordingFlakyClient()
    gateway = SandboxPayPal(settings(), client=fake, retry_delay=0)
    gateway.get_invoice("INV-1")
    assert "PayPal-Request-Id" not in fake.attempts[-1][2]


def _create(gateway):
    return gateway.create_invoice(
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


def test_a_retried_create_reuses_its_request_id_after_a_429():
    """Create is the endpoint PayPal documents the header for."""

    class RateLimited(RecordingFlakyClient):
        def request(self, method, url, **kwargs):
            self.attempts.append((method, url, dict(kwargs.get("headers") or {})))
            if self.failures:
                self.failures -= 1
                return FakeResponse(status_code=429, text="slow down")
            return FakeClient.request(self, method, url, **kwargs)

    fake = RateLimited(failures=1)
    assert _create(SandboxPayPal(settings(), client=fake, retry_delay=0)) == "INV-1"
    ids = [headers["PayPal-Request-Id"] for _, url, headers in fake.attempts if url == "/v2/invoicing/invoices"]
    assert len(ids) == 2 and ids[0] == ids[1]


def test_a_retried_payment_reuses_its_request_id_after_a_transport_error():
    class Dropped(RecordingFlakyClient):
        def request(self, method, url, **kwargs):
            self.attempts.append((method, url, dict(kwargs.get("headers") or {})))
            if self.failures:
                self.failures -= 1
                raise httpx.ConnectError("network down")
            return FakeClient.request(self, method, url, **kwargs)

    fake = Dropped(failures=2)
    SandboxPayPal(settings(), client=fake, retry_delay=0).record_payment("INV-1", 120000, "USD")
    ids = [headers["PayPal-Request-Id"] for _, url, headers in fake.attempts if url.endswith("/payments")]
    assert len(ids) == 3 and len(set(ids)) == 1


def test_two_creates_get_different_request_ids():
    fake = RecordingFlakyClient()
    gateway = SandboxPayPal(settings(), client=fake, retry_delay=0)
    _create(gateway)
    _create(gateway)
    ids = [headers["PayPal-Request-Id"] for _, url, headers in fake.attempts if url == "/v2/invoicing/invoices"]
    assert len(ids) == 2 and ids[0] != ids[1]
