"""A draft whose send failed can be sent again without a second PayPal invoice (issue #41)."""

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlmodel import Session, select

from app.models import Invoice, Schedule, init_db, make_engine
from app.paypal import PayPalError, PayPalTimeout
from app.services import SEND_UNCONFIRMED
from tests.conftest import sign_in
from tests.test_approve_once import _raise
from tests.test_schedules import FORM, items


def _failed_send(client, **extra) -> dict:
    """Submit the form while PayPal refuses the send. Returns the DRAFT row."""
    gateway = client.app.state.gateway
    real_send = gateway.send_invoice
    gateway.send_invoice = _raise(PayPalError("PayPal is down."))
    response = client.post("/invoices", data=dict(FORM, **extra), follow_redirects=False)
    assert response.status_code == 502
    gateway.send_invoice = real_send
    [row] = client.get("/api/invoices").json()
    assert row["status"] == "DRAFT" and row["paypal_invoice_id"]
    return row


def test_send_sends_the_draft_paypal_already_holds(client):
    row = _failed_send(client)
    gateway = client.app.state.gateway
    calls = []
    real_send = gateway.send_invoice
    gateway.send_invoice = lambda paypal_id: calls.append(paypal_id) or real_send(paypal_id)

    response = client.post(f"/api/invoices/{row['id']}/send")
    assert response.status_code == 200, response.text
    assert response.json() == {"ok": True, "status": "SENT"}
    assert calls == [row["paypal_invoice_id"]]
    assert len(gateway.invoices) == 1, "the send reuses the PayPal invoice"
    [after] = client.get("/api/invoices").json()
    assert after["status"] == "SENT"
    assert after["paypal_invoice_id"] == row["paypal_invoice_id"]


def test_send_refuses_an_invoice_that_is_not_an_unsent_draft(client):
    client.post("/invoices", data=FORM, follow_redirects=False)
    [row] = client.get("/api/invoices").json()
    assert row["status"] == "SENT"
    assert client.post(f"/api/invoices/{row['id']}/send").status_code == 409


def test_send_another_users_draft_reads_as_missing(client):
    row = _failed_send(client)
    other = sign_in(TestClient(client.app), email="other@duebell.test")
    assert other.post(f"/api/invoices/{row['id']}/send").status_code == 404
    [after] = client.get("/api/invoices").json()
    assert after["status"] == "DRAFT"


def test_send_failure_returns_502_and_keeps_the_draft(client):
    row = _failed_send(client)
    gateway = client.app.state.gateway
    gateway.send_invoice = _raise(PayPalError("PayPal is still down."))
    response = client.post(f"/api/invoices/{row['id']}/send")
    assert response.status_code == 502
    assert "still down" in response.json()["error"]
    [after] = client.get("/api/invoices").json()
    assert after["status"] == "DRAFT"
    assert len(gateway.invoices) == 1


def test_send_timeout_counts_as_sent_and_warns(client):
    row = _failed_send(client)
    client.app.state.gateway.send_invoice = _raise(PayPalTimeout("PayPal did not answer in time."))
    response = client.post(f"/api/invoices/{row['id']}/send")
    assert response.status_code == 200
    assert response.json()["warning"] == SEND_UNCONFIRMED
    [after] = client.get("/api/invoices").json()
    assert after["status"] == "SENT"


def test_a_ticked_repeat_becomes_a_schedule_on_the_first_successful_send(client):
    row = _failed_send(client, repeat="MONTH", repeat_cycles="2")
    assert items(client) == [], "no schedule while the invoice is unsent"

    # A second failure keeps the repeat waiting.
    gateway = client.app.state.gateway
    real_send = gateway.send_invoice
    gateway.send_invoice = _raise(PayPalError("PayPal is still down."))
    assert client.post(f"/api/invoices/{row['id']}/send").status_code == 502
    assert items(client) == []

    gateway.send_invoice = real_send
    assert client.post(f"/api/invoices/{row['id']}/send").status_code == 200
    [upcoming] = items(client)
    assert upcoming.status == "UPCOMING"
    with Session(client.app.state.engine) as session:
        [schedule] = session.exec(select(Schedule)).all()
        assert schedule.interval_unit == "MONTH"
        assert schedule.cycles_left == 1, "two repeats: one queued, one left"
        assert schedule.user_id == client.user_id
        invoice = session.get(Invoice, row["id"])
        assert invoice.repeat_unit is None, "the repeat is used once"

    # Sending again is refused and adds no second schedule.
    assert client.post(f"/api/invoices/{row['id']}/send").status_code == 409
    assert len(items(client)) == 1


def test_a_ticked_repeat_becomes_a_schedule_when_the_send_times_out(client):
    row = _failed_send(client, repeat="WEEK")
    client.app.state.gateway.send_invoice = _raise(PayPalTimeout("PayPal did not answer in time."))
    assert client.post(f"/api/invoices/{row['id']}/send").status_code == 200
    with Session(client.app.state.engine) as session:
        [schedule] = session.exec(select(Schedule)).all()
        assert schedule.interval_unit == "WEEK"
        assert schedule.cycles_left is None


def test_the_dashboard_offers_send_for_an_unsent_draft(client):
    _failed_send(client)
    page = client.get("/")
    assert '"status": "DRAFT"' in page.text
    script = client.get("/static/dashboard.js").text
    assert 'data-action="send"' in script


def test_an_older_database_gains_the_repeat_columns(tmp_path):
    url = f"sqlite:///{tmp_path / 'old.db'}"
    engine = make_engine(url)
    init_db(engine)
    with engine.begin() as connection:
        connection.execute(text('ALTER TABLE "invoice" DROP COLUMN "repeat_unit"'))
        connection.execute(text('ALTER TABLE "invoice" DROP COLUMN "repeat_cycles"'))
    init_db(create_engine(url))
    with engine.connect() as connection:
        columns = {row[1] for row in connection.execute(text('PRAGMA table_info("invoice")'))}
    assert {"repeat_unit", "repeat_cycles"} <= columns
