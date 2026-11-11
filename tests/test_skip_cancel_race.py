"""Skip and Cancel change a scheduled item only if it is still as they read it (issue #117)."""

import threading

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session, select

from app import schedules
from app.config import Settings
from app.models import Invoice, Schedule, ScheduledInvoice
from app.paypal import PayPalError
from app.schedules import cancel_schedule, reject_scheduled
from tests.conftest import PASSWORD
from tests.test_schedules import items, plan_form


@pytest.fixture
def settings(tmp_path):
    """A file database, so a second tab can write while the first waits on PayPal."""
    return Settings(
        database_url=f"sqlite:///{tmp_path / 'skip-cancel.db'}",
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


def _second_tab(client):
    tab = TestClient(client.app)
    tab.post("/login", data={"email": "owner@duebell.test", "password": PASSWORD})
    return tab


def _approve_in_background(client, item_id):
    results = {}
    worker = threading.Thread(
        target=lambda: results.setdefault("approve", client.post(f"/api/scheduled/{item_id}/approve"))
    )
    worker.start()
    return worker, results


def test_skip_during_an_approval_gets_409_and_the_item_ends_as_the_approval_left_it(client):
    _, item = _plan(client)
    gateway = client.app.state.gateway
    inside_paypal, let_go = threading.Event(), threading.Event()

    def failing_send(paypal_id):
        inside_paypal.set()
        let_go.wait(timeout=5)
        raise PayPalError("PayPal is down.")

    gateway.send_invoice = failing_send
    # A Skip that read the item while it was still pending, before the approval claimed it.
    skip_session = Session(client.app.state.engine)
    skipped_item = skip_session.get(ScheduledInvoice, item.id)
    assert skipped_item.status == "PENDING"

    worker, results = _approve_in_background(client, item.id)
    assert inside_paypal.wait(timeout=5)
    assert _second_tab(client).post(f"/api/scheduled/{item.id}/reject").status_code == 409
    with pytest.raises(ValueError, match="no longer pending"):
        reject_scheduled(skip_session, skipped_item)
    skip_session.close()
    let_go.set()
    worker.join(timeout=5)

    assert results["approve"].status_code == 502
    after = items(client)[0]
    assert after.status == "PENDING", "the failed approval gave the item back"
    with Session(client.app.state.engine) as session:
        draft = session.get(Invoice, after.invoice_id)
        assert draft.status == "DRAFT" and draft.paypal_invoice_id, "PayPal holds the unsent draft"
    # The draft PayPal holds still blocks a Skip, as before (issue #79).
    assert client.post(f"/api/scheduled/{item.id}/reject").status_code == 409
    assert items(client)[0].status == "PENDING"


def test_cancel_racing_a_successful_approval_leaves_the_item_sent(client, monkeypatch):
    schedule_id, item = _plan(client)
    gateway = client.app.state.gateway
    real_create = gateway.create_invoice
    inside_paypal, let_go = threading.Event(), threading.Event()

    def slow_create(**facts):
        inside_paypal.set()
        let_go.wait(timeout=5)
        return real_create(**facts)

    gateway.create_invoice = slow_create
    real_open_items = schedules._open_items
    cancel_has_read, approval_done = threading.Event(), threading.Event()

    def open_items_then_wait(session, schedule):
        found = real_open_items(session, schedule)
        if not cancel_has_read.is_set():
            # Cancel has read the item as being sent; the approval finishes before it writes.
            cancel_has_read.set()
            approval_done.wait(timeout=5)
        return found

    monkeypatch.setattr(schedules, "_open_items", open_items_then_wait)
    worker, results = _approve_in_background(client, item.id)
    assert inside_paypal.wait(timeout=5)

    def cancel():
        with Session(client.app.state.engine) as session:
            cancel_schedule(session, session.get(Schedule, schedule_id))

    canceller = threading.Thread(target=cancel)
    canceller.start()
    assert cancel_has_read.wait(timeout=5)
    let_go.set()
    worker.join(timeout=5)
    approval_done.set()
    canceller.join(timeout=5)

    assert results["approve"].status_code == 200
    [first, second] = items(client)
    assert first.status == "SENT", "Cancel never overwrites a sent item"
    assert second.status == "CANCELLED"
    with Session(client.app.state.engine) as session:
        assert session.get(Schedule, schedule_id).status == "CANCELLED"
    assert [facts["status"] for facts in gateway.invoices.values()] == ["SENT"]
