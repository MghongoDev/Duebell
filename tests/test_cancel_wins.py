"""Cancelling a schedule wins over an approval in flight (issue #77, ADR 0007)."""

import threading

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session, select

from app.config import Settings
from app.models import Invoice, Schedule
from app.paypal import PayPalError
from app.schedules import cancel_schedule, schedule_rows
from tests.conftest import PASSWORD
from tests.test_schedules import instalment_invoices, items, plan_form


@pytest.fixture
def settings(tmp_path):
    """A file database, so a second tab can write while the first waits on PayPal."""
    return Settings(
        database_url=f"sqlite:///{tmp_path / 'cancel.db'}",
        mock_webhooks=True,
        amount_cap_cents=1_000_000,
        allowed_currencies=("USD",),
        terms_min_days=0,
        terms_max_days=120,
    )


def _plan(client):
    client.post("/invoices", data=plan_form(client, instalments="2"), follow_redirects=False)
    with Session(client.app.state.engine) as session:
        schedule_id = session.exec(select(Schedule)).one().id
    return schedule_id, items(client)[0]


def _cancel(client, schedule_id):
    with Session(client.app.state.engine) as session:
        cancel_schedule(session, session.get(Schedule, schedule_id))


def _pending_rows(client):
    with Session(client.app.state.engine) as session:
        return schedule_rows(session, client.user_id)["pending"]


def test_a_cancel_during_a_failed_create_keeps_the_item_cancelled(client):
    schedule_id, item = _plan(client)
    second_tab = TestClient(client.app)
    second_tab.post("/login", data={"email": "owner@duebell.test", "password": PASSWORD})
    gateway = client.app.state.gateway
    inside_paypal, let_go = threading.Event(), threading.Event()

    def failing_create(**facts):
        inside_paypal.set()
        let_go.wait(timeout=5)
        raise PayPalError("PayPal is down.")

    gateway.create_invoice = failing_create
    results = {}
    worker = threading.Thread(
        target=lambda: results.setdefault("approve", client.post(f"/api/scheduled/{item.id}/approve"))
    )
    worker.start()
    assert inside_paypal.wait(timeout=5)
    assert second_tab.post(f"/api/schedules/{schedule_id}/cancel").status_code == 200
    let_go.set()
    worker.join(timeout=5)

    assert results["approve"].status_code == 502
    [after, _] = items(client)
    assert after.status == "CANCELLED"
    assert after.invoice_id is None
    with Session(client.app.state.engine) as session:
        assert instalment_invoices(session) == [], "the draft PayPal never took is deleted"
    assert _pending_rows(client) == []
    assert client.post(f"/api/scheduled/{item.id}/approve").status_code == 409
    assert gateway.invoices == {}


def test_a_cancel_during_a_failed_send_keeps_the_item_cancelled_and_the_draft(client):
    schedule_id, item = _plan(client)
    gateway = client.app.state.gateway

    def failing_send(paypal_id):
        _cancel(client, schedule_id)
        raise PayPalError("PayPal is down.")

    gateway.send_invoice = failing_send
    assert client.post(f"/api/scheduled/{item.id}/approve").status_code == 502

    after = items(client)[0]
    assert after.status == "CANCELLED"
    with Session(client.app.state.engine) as session:
        draft = session.get(Invoice, after.invoice_id)
        assert draft.status == "DRAFT" and draft.paypal_invoice_id, "the unsent draft stays for the user"
    assert _pending_rows(client) == []
    assert client.post(f"/api/scheduled/{item.id}/approve").status_code == 409
    assert [facts["status"] for facts in gateway.invoices.values()] == ["DRAFT"]


def test_an_item_of_a_cancelled_schedule_is_not_approved_or_listed(client):
    """A pending item left on a cancelled schedule, as an older build could leave it."""
    schedule_id, item = _plan(client)
    with Session(client.app.state.engine) as session:
        schedule = session.get(Schedule, schedule_id)
        schedule.status = "CANCELLED"
        session.add(schedule)
        session.commit()
    assert items(client)[0].status == "PENDING"

    response = client.post(f"/api/scheduled/{item.id}/approve")
    assert response.status_code == 409
    assert items(client)[0].status == "PENDING"
    assert _pending_rows(client) == []
    assert client.app.state.gateway.invoices == {}
