"""Agent Toolkit gateway adapter (issue #21).

A fake toolkit stands in for paypal_agent_toolkit, so these tests need neither
the package nor a network. The fake records each (method, params) pair the
adapter hands to PayPalAPI.run.
"""

import json
import sys
import time
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.models import make_engine
from app.paypal import PayPalError, ToolkitPayPal, build_gateway
from app.web import create_app
from tests.conftest import sign_in
from tests.test_paypal_sandbox import NOW, FakeClient


class FakeToolkit:
    def __init__(self, status="SENT", error=None, delay=0.0):
        self.calls = []
        self.status = status
        self.error = error
        self.delay = delay

    def run(self, method, params):
        self.calls.append((method, params))
        if self.delay:
            time.sleep(self.delay)
        if self.error:
            raise self.error
        if method == "get_invoice":
            return json.dumps({"id": params["invoice_id"], "status": self.status})
        if method == "send_invoice_reminder":
            return {"success": True, "invoice_id": params["invoice_id"]}
        return json.dumps({})


def settings(**overrides):
    values = {
        "paypal_mode": "toolkit",
        "paypal_client_id": "client-id",
        "paypal_client_secret": "client-secret",
        "paypal_base_url": "https://api.test",
        "database_url": "sqlite://",
        "paypal_timeout_seconds": 2.0,
    }
    values.update(overrides)
    return Settings(**values)


def gateway(toolkit=None, **overrides):
    return ToolkitPayPal(settings(**overrides), toolkit=toolkit or FakeToolkit(), client=FakeClient(), retry_delay=0)


def test_send_remind_pay_and_status_go_through_the_toolkit():
    toolkit = FakeToolkit(status="PAID")
    gw = gateway(toolkit)
    gw.send_invoice("INV-1")
    gw.send_reminder("INV-1", "Reminder: invoice DUE-1", "Please pay.")
    gw.record_payment("INV-1", 120000, "EUR")
    assert gw.get_invoice("INV-1") == "PAID"
    methods = [method for method, _ in toolkit.calls]
    assert methods == ["send_invoice", "send_invoice_reminder", "record_payment_for_invoice", "get_invoice"]
    assert toolkit.calls[0][1] == {"invoice_id": "INV-1", "send_to_recipient": True}
    assert toolkit.calls[1][1] == {"invoice_id": "INV-1", "subject": "Reminder: invoice DUE-1", "note": "Please pay."}
    payment = toolkit.calls[2][1]
    assert payment["method"] == "BANK_TRANSFER"
    assert payment["amount"] == {"currency_code": "EUR", "value": "1200.00"}


def test_create_keeps_payment_terms_on_the_rest_path():
    toolkit = FakeToolkit()
    gw = gateway(toolkit)
    paypal_id = gw.create_invoice(
        invoice_number="DUE-1",
        amount_cents=120000,
        currency="USD",
        description="Logo design",
        terms_days=14,
        client_name="Acme Studio",
        client_email="billing@acme.test",
        issued_at=NOW,
        due_at=NOW + timedelta(days=14),
    )
    assert paypal_id == "INV-1"
    assert gw.client.last_json["detail"]["payment_term"] == {"term_type": "NET_15"}
    assert toolkit.calls == []


def test_toolkit_errors_become_readable_paypal_errors():
    gw = gateway(FakeToolkit(error=RuntimeError("Failed to obtain access token from PayPal")))
    with pytest.raises(PayPalError, match=r"send_invoice\): Failed to obtain access token"):
        gw.send_invoice("INV-1")


def test_a_hung_toolkit_call_times_out():
    gw = gateway(FakeToolkit(delay=0.5), paypal_timeout_seconds=0.05)
    with pytest.raises(PayPalError, match="did not answer within 0.05 seconds"):
        gw.get_invoice("INV-1")


def test_webhook_verification_stays_on_the_rest_path():
    toolkit = FakeToolkit()
    gw = gateway(toolkit, paypal_webhook_id="WH-1")
    gw.verify_webhook({"paypal-transmission-id": "t"}, json.dumps({"id": "EV-1"}))
    assert toolkit.calls == []
    assert gw.client.requests[-1][1] == "/v1/notifications/verify-webhook-signature"


def test_missing_package_gives_install_hint(monkeypatch):
    monkeypatch.setitem(sys.modules, "paypal_agent_toolkit", None)
    with pytest.raises(PayPalError, match="requirements-toolkit.txt"):
        build_gateway(settings())


def test_demo_flow_in_toolkit_mode():
    toolkit = FakeToolkit(status="PAID")
    gw = gateway(toolkit)
    s = settings()
    app = create_app(settings=s, engine=make_engine(s.database_url), gateway=gw)
    client = sign_in(TestClient(app))
    form = {
        "client_name": "Acme Studio",
        "client_email": "billing@acme.test",
        "description": "Logo design",
        "amount": "1200.00",
        "currency": "USD",
        "terms_days": "2",
    }
    assert client.post("/invoices", data=form, follow_redirects=False).status_code == 303
    invoice = client.get("/api/invoices").json()[0]
    assert invoice["status"] == "SENT"

    assert client.post("/api/reminders/scan").json()["created"] == 1
    reminder = client.get("/api/reminders").json()[0]
    approved = client.post(f"/api/reminders/{reminder['id']}/approve").json()
    assert approved["status"] == "SENT"

    refreshed = client.post(f"/api/invoices/{invoice['id']}/refresh").json()
    assert refreshed == {"ok": True, "status": "PAID"}
    methods = [method for method, _ in toolkit.calls]
    assert methods == ["send_invoice", "send_invoice_reminder", "get_invoice"]
