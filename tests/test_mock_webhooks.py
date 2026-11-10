"""Mock mode refuses webhooks unless MOCK_WEBHOOKS is on (issue #38)."""

import json
from dataclasses import replace
from pathlib import Path

from fastapi.testclient import TestClient
from sqlmodel import Session, select

from app.config import Settings, get_settings
from app.models import Event, make_engine
from app.paypal import SandboxPayPal
from app.services import handle_webhook
from app.web import create_app
from tests.conftest import sign_in

FORM = {
    "client_name": "Acme Studio",
    "client_email": "billing@acme.test",
    "description": "Logo design",
    "amount": "1200.00",
    "currency": "USD",
    "terms_days": "14",
}


def paid_event(event_id, paypal_invoice_id):
    return json.dumps(
        {"id": event_id, "event_type": "INVOICING.INVOICE.PAID", "resource": {"id": paypal_invoice_id}}
    )


def hosted_client(settings):
    """A signed-in client for an app built the way the hosted demo builds it."""
    app = create_app(settings=replace(settings, mock_webhooks=False), engine=make_engine("sqlite://"))
    return sign_in(TestClient(app))


def test_mock_webhooks_are_off_by_default():
    assert Settings().mock_webhooks is False


def test_mock_webhooks_setting_reads_the_environment(monkeypatch):
    monkeypatch.setenv("MOCK_WEBHOOKS", "true")
    get_settings.cache_clear()
    try:
        assert get_settings().mock_webhooks is True
    finally:
        get_settings.cache_clear()


def test_mock_mode_refuses_webhooks_when_the_setting_is_off(settings):
    client = hosted_client(settings)
    client.post("/invoices", data=FORM, follow_redirects=False)
    row = client.get("/api/invoices").json()[0]

    response = TestClient(client.app).post("/webhooks/paypal", content=paid_event("WH-OFF", row["paypal_invoice_id"]))

    assert response.status_code == 401
    assert response.json()["ok"] is False
    assert client.get("/api/invoices").json()[0]["status"] == "SENT"
    with Session(client.app.state.engine) as session:
        assert session.exec(select(Event)).all() == []


def test_mock_mode_accepts_webhooks_when_the_setting_is_on(client):
    client.post("/invoices", data=FORM, follow_redirects=False)
    row = client.get("/api/invoices").json()[0]
    response = client.post("/webhooks/paypal", content=paid_event("WH-ON", row["paypal_invoice_id"]))
    assert response.status_code == 200
    assert response.json()["applied"] is True


class VerifiedClient:
    """Answers PayPal's signature check with SUCCESS and nothing else."""

    def post(self, url, **kwargs):
        return FakeResponse({"access_token": "token", "expires_in": 3000})

    def request(self, method, url, **kwargs):
        return FakeResponse({"verification_status": "SUCCESS"})


class FakeResponse:
    status_code = 200
    headers: dict = {}
    text = ""

    def __init__(self, data):
        self._data = data

    def json(self):
        return self._data


def test_sandbox_still_checks_the_signature_with_the_setting_off(settings):
    sandbox = Settings(
        paypal_mode="sandbox",
        paypal_client_id="client-id",
        paypal_client_secret="client-secret",
        paypal_webhook_id="WH-ID",
        paypal_base_url="https://api.test",
    )
    assert sandbox.mock_webhooks is False
    gateway = SandboxPayPal(sandbox, client=VerifiedClient())
    engine = make_engine("sqlite://")
    create_app(settings=settings, engine=engine)  # creates the tables
    with Session(engine) as session:
        result = handle_webhook(session, gateway, {}, paid_event("WH-SANDBOX", "INV-NONE"))
        assert result["ok"] is True
        assert session.get(Event, "WH-SANDBOX") is not None


def test_render_blueprint_leaves_mock_webhooks_off():
    blueprint = (Path(__file__).resolve().parents[1] / "render.yaml").read_text()
    assert "MOCK_WEBHOOKS" not in blueprint
