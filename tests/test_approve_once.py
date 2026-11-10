"""A scheduled invoice is created and sent at most once (issue #39)."""

import threading

from fastapi.testclient import TestClient
from sqlmodel import Session, select

from app.config import Settings
from app.models import Invoice, ScheduledInvoice, make_engine, utcnow
from app.paypal import PayPalError, PayPalTimeout
from app.services import SEND_UNCONFIRMED, create_invoice
from app.validation import InvoiceProposal
from app.web import create_app
from tests.conftest import PASSWORD, add_late_payer, sign_in
from tests.test_schedules import FORM, items, plan_form


def _raise(exc):
    def fail(*args, **kwargs):
        raise exc

    return fail


def test_two_approvals_at_once_create_one_paypal_invoice(tmp_path):
    settings = Settings(
        database_url=f"sqlite:///{tmp_path / 'race.db'}",
        amount_cap_cents=1_000_000,
        allowed_currencies=("USD",),
        terms_min_days=0,
        terms_max_days=120,
    )
    app = create_app(settings=settings, engine=make_engine(settings.database_url))
    first_tab = sign_in(TestClient(app))
    second_tab = TestClient(app)
    second_tab.post("/login", data={"email": "owner@duebell.test", "password": PASSWORD})
    first_tab.post("/invoices", data=plan_form(first_tab, instalments="2"), follow_redirects=False)
    item = items(first_tab)[0]

    gateway = app.state.gateway
    real_create = gateway.create_invoice
    inside_paypal, let_go = threading.Event(), threading.Event()

    def slow_create(**facts):
        inside_paypal.set()
        let_go.wait(timeout=5)
        return real_create(**facts)

    gateway.create_invoice = slow_create
    results = {}
    worker = threading.Thread(
        target=lambda: results.setdefault("first", first_tab.post(f"/api/scheduled/{item.id}/approve"))
    )
    worker.start()
    assert inside_paypal.wait(timeout=5)
    second = second_tab.post(f"/api/scheduled/{item.id}/approve")
    let_go.set()
    worker.join(timeout=5)

    assert second.status_code == 409
    assert results["first"].status_code == 200
    assert len(gateway.invoices) == 1
    assert items(first_tab)[0].status == "SENT"


def test_a_taken_number_moves_the_new_invoice_to_the_next_one(client):
    """Deleting an invoice used to let count+1 repeat a number that still existed."""
    engine = client.app.state.engine
    with Session(engine) as session:
        add_late_payer(client)
        taken = f"DUE-{utcnow():%Y%m%d}-{_highest_id(session) + 1:04d}"
        first = session.exec(select(Invoice)).first()
        first.invoice_number = taken
        session.add(first)
        session.commit()

        proposal = InvoiceProposal(
            client_name="Acme Studio",
            client_email="billing@acme.test",
            description="Logo",
            amount="100.00",
            currency="USD",
            terms_days=14,
        )
        invoice = create_invoice(
            session, proposal, client.app.state.settings, client.app.state.gateway, user_id=client.user_id
        )

    assert invoice.invoice_number != taken
    assert invoice.status == "SENT"


def _highest_id(session):
    return max(invoice.id for invoice in session.exec(select(Invoice)).all())


def test_an_item_whose_invoice_paypal_already_sent_is_marked_sent(client):
    client.post("/invoices", data=plan_form(client, instalments="2"), follow_redirects=False)
    item = items(client)[0]
    gateway = client.app.state.gateway
    real_send = gateway.send_invoice
    gateway.send_invoice = _raise(PayPalError("PayPal is down."))
    assert client.post(f"/api/scheduled/{item.id}/approve").status_code == 502

    # PayPal sent it after all, and a webhook said so.
    with Session(client.app.state.engine) as session:
        invoice = session.get(Invoice, items(client)[0].invoice_id)
        invoice.status = "SENT"
        session.add(invoice)
        session.commit()
    calls = []
    gateway.send_invoice = lambda paypal_id: calls.append(paypal_id) or real_send(paypal_id)

    response = client.post(f"/api/scheduled/{item.id}/approve")
    assert response.status_code == 200
    assert response.json()["status"] == "SENT"
    assert calls == [], "an invoice PayPal has sent is not sent again"
    assert len(gateway.invoices) == 1


def test_a_send_timeout_counts_as_sent_and_warns(client):
    client.post("/invoices", data=plan_form(client, instalments="2"), follow_redirects=False)
    item = items(client)[0]
    gateway = client.app.state.gateway
    gateway.send_invoice = _raise(PayPalTimeout("PayPal did not answer in time."))

    response = client.post(f"/api/scheduled/{item.id}/approve")
    assert response.status_code == 200
    assert response.json()["warning"] == SEND_UNCONFIRMED
    assert items(client)[0].status == "SENT"
    assert client.post(f"/api/scheduled/{item.id}/approve").status_code == 409
    assert len(gateway.invoices) == 1


def test_a_send_timeout_on_the_form_shows_a_notice(client):
    client.app.state.gateway.send_invoice = _raise(PayPalTimeout("PayPal did not answer in time."))
    response = client.post("/invoices", data=FORM, follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/?notice=send-unconfirmed"
    assert SEND_UNCONFIRMED in client.get(response.headers["location"]).text
    [row] = client.get("/api/invoices").json()
    assert row["status"] == "SENT"


def test_the_page_shows_no_text_from_the_url(client):
    assert "planted" not in client.get("/?notice=planted").text


def test_an_approval_a_restart_cut_short_is_pending_again(client, settings):
    client.post("/invoices", data=plan_form(client, instalments="2"), follow_redirects=False)
    engine = client.app.state.engine
    with Session(engine) as session:
        item = session.get(ScheduledInvoice, items(client)[0].id)
        item.status = "SENDING"
        session.add(item)
        session.commit()

    create_app(settings=settings, engine=engine)
    assert items(client)[0].status == "PENDING"
