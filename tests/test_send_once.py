"""One request at a time sends a linked draft: its row Send or its item's approval (issue #104)."""

import threading

from fastapi.testclient import TestClient

from app.config import Settings
from app.models import make_engine
from app.paypal import PayPalError
from app.web import create_app
from tests.conftest import PASSWORD, sign_in
from tests.test_approve_once import _raise
from tests.test_linked_draft import _invoice, _last_instalment_with_unsent_draft, _schedule
from tests.test_retry_send import _failed_send
from tests.test_schedules import items


def _two_tabs(tmp_path):
    settings = Settings(
        database_url=f"sqlite:///{tmp_path / 'send-once.db'}",
        amount_cap_cents=1_000_000,
        allowed_currencies=("USD",),
        terms_min_days=0,
        terms_max_days=120,
    )
    app = create_app(settings=settings, engine=make_engine(settings.database_url))
    first_tab = sign_in(TestClient(app))
    second_tab = TestClient(app)
    second_tab.post("/login", data={"email": "owner@duebell.test", "password": PASSWORD})
    return first_tab, second_tab


def _pause_sends(gateway):
    """Hold the first PayPal send until let_go is set. Returns the events and the list of sends."""
    real_send = gateway.send_invoice
    inside_paypal, let_go = threading.Event(), threading.Event()
    sends = []

    def slow_send(paypal_id):
        sends.append(paypal_id)
        inside_paypal.set()
        let_go.wait(timeout=5)
        return real_send(paypal_id)

    gateway.send_invoice = slow_send
    return inside_paypal, let_go, sends


def _in_background(results, name, call):
    worker = threading.Thread(target=lambda: results.setdefault(name, call()))
    worker.start()
    return worker


def test_send_from_the_row_is_refused_while_the_item_is_being_approved(tmp_path):
    first_tab, second_tab = _two_tabs(tmp_path)
    item, draft = _last_instalment_with_unsent_draft(first_tab)
    inside_paypal, let_go, sends = _pause_sends(first_tab.app.state.gateway)

    results = {}
    worker = _in_background(results, "approve", lambda: first_tab.post(f"/api/scheduled/{item.id}/approve"))
    assert inside_paypal.wait(timeout=5)
    row_send = second_tab.post(f"/api/invoices/{draft.id}/send")
    let_go.set()
    worker.join(timeout=5)

    assert row_send.status_code == 409
    assert "already being" in row_send.json()["error"]
    assert results["approve"].status_code == 200
    assert sends == [draft.paypal_invoice_id], "PayPal got one send"
    assert items(first_tab)[1].status == "SENT"
    assert _invoice(first_tab, draft.id).status == "SENT"
    assert _schedule(first_tab).status == "DONE"


def test_approval_is_refused_while_the_row_sends_the_linked_draft(tmp_path):
    first_tab, second_tab = _two_tabs(tmp_path)
    item, draft = _last_instalment_with_unsent_draft(first_tab)
    inside_paypal, let_go, sends = _pause_sends(first_tab.app.state.gateway)

    results = {}
    worker = _in_background(results, "send", lambda: first_tab.post(f"/api/invoices/{draft.id}/send"))
    assert inside_paypal.wait(timeout=5)
    approval = second_tab.post(f"/api/scheduled/{item.id}/approve")
    let_go.set()
    worker.join(timeout=5)

    assert approval.status_code == 409
    assert results["send"].status_code == 200
    assert results["send"].json() == {"ok": True, "status": "SENT"}
    assert sends == [draft.paypal_invoice_id], "PayPal got one send"
    assert items(first_tab)[1].status == "SENT"
    assert _schedule(first_tab).status == "DONE"


def test_a_failed_row_send_gives_the_item_back_for_approval(client):
    item, draft = _last_instalment_with_unsent_draft(client)
    gateway = client.app.state.gateway
    real_send = gateway.send_invoice
    gateway.send_invoice = _raise(PayPalError("PayPal is down."))

    assert client.post(f"/api/invoices/{draft.id}/send").status_code == 502
    assert items(client)[1].status == "PENDING"

    gateway.send_invoice = real_send
    assert client.post(f"/api/scheduled/{item.id}/approve").status_code == 200
    assert items(client)[1].status == "SENT"


def test_send_on_a_draft_with_no_scheduled_item_is_unchanged(client):
    row = _failed_send(client)

    response = client.post(f"/api/invoices/{row['id']}/send")

    assert response.status_code == 200
    assert response.json() == {"ok": True, "status": "SENT"}
    assert items(client) == []
