"""A scheduled item is linked to its invoice before PayPal is called (issue #74)."""

import pytest
from sqlalchemy.exc import OperationalError
from sqlmodel import Session

from app.models import Invoice, ScheduledInvoice
from app.paypal import PayPalError
from app.schedules import approve_scheduled
from app.web import create_app
from tests.test_schedules import instalment_invoices, items, plan_form


class Crash(BaseException):
    """The process stops here. No `except Exception` catches it."""


def _first_item(client):
    client.post("/invoices", data=plan_form(client, instalments="2"), follow_redirects=False)
    return items(client)[0]


def _approve_until_crash(client, item_id):
    app = client.app
    with Session(app.state.engine) as session:
        item = session.get(ScheduledInvoice, item_id)
        with pytest.raises(Crash):
            approve_scheduled(session, item, app.state.settings, app.state.gateway)


def _sent_in_paypal(gateway):
    return sum(1 for facts in gateway.invoices.values() if facts["status"] == "SENT")


def test_the_item_holds_its_invoice_before_paypal_is_called(client):
    item = _first_item(client)
    gateway = client.app.state.gateway
    real_create = gateway.create_invoice
    seen = []

    def create(**facts):
        seen.append(items(client)[0].invoice_id)
        return real_create(**facts)

    gateway.create_invoice = create
    response = client.post(f"/api/scheduled/{item.id}/approve")

    assert response.status_code == 200
    assert seen == [response.json()["invoice_id"]]


def test_a_commit_failure_after_paypal_sends_then_a_retry_gives_one_invoice(client, monkeypatch):
    item = _first_item(client)
    gateway = client.app.state.gateway

    def send_then_fail(session, invoice, gateway, **kwargs):
        gateway.send_invoice(invoice.paypal_invoice_id)
        raise OperationalError("COMMIT", {}, Exception("database is locked"))

    monkeypatch.setattr("app.services.send_unsent_invoice", send_then_fail)
    with pytest.raises(OperationalError):
        client.post(f"/api/scheduled/{item.id}/approve")
    monkeypatch.undo()
    assert items(client)[0].status == "PENDING"
    assert items(client)[0].invoice_id is not None

    assert client.post(f"/api/scheduled/{item.id}/approve").status_code == 200
    assert len(gateway.invoices) == 1
    assert _sent_in_paypal(gateway) == 1
    with Session(client.app.state.engine) as session:
        assert len(instalment_invoices(session)) == 1
    assert items(client)[0].status == "SENT"


def test_a_restart_after_paypal_sends_then_a_retry_gives_one_invoice(client, settings):
    item = _first_item(client)
    gateway = client.app.state.gateway
    real_send = gateway.send_invoice

    def send_then_stop(paypal_id):
        real_send(paypal_id)
        raise Crash()

    gateway.send_invoice = send_then_stop
    _approve_until_crash(client, item.id)
    gateway.send_invoice = real_send

    create_app(settings=settings, engine=client.app.state.engine)
    assert items(client)[0].status == "PENDING"
    assert client.post(f"/api/scheduled/{item.id}/approve").status_code == 200
    assert len(gateway.invoices) == 1
    assert _sent_in_paypal(gateway) == 1
    with Session(client.app.state.engine) as session:
        assert len(instalment_invoices(session)) == 1


def test_a_restart_before_paypal_creates_the_draft_reuses_it(client, settings):
    item = _first_item(client)
    gateway = client.app.state.gateway
    real_create = gateway.create_invoice

    def stop(**facts):
        raise Crash()

    gateway.create_invoice = stop
    _approve_until_crash(client, item.id)
    gateway.create_invoice = real_create
    linked = items(client)[0].invoice_id
    assert linked is not None

    create_app(settings=settings, engine=client.app.state.engine)
    response = client.post(f"/api/scheduled/{item.id}/approve")
    assert response.status_code == 200
    assert response.json()["invoice_id"] == linked
    assert len(gateway.invoices) == 1
    with Session(client.app.state.engine) as session:
        [invoice] = instalment_invoices(session)
        assert invoice.status == "SENT"
        assert invoice.paypal_invoice_id in gateway.invoices


def test_paypal_refusing_to_create_drops_the_draft_and_the_link(client):
    item = _first_item(client)
    gateway = client.app.state.gateway
    real_create = gateway.create_invoice

    def refuse(**facts):
        raise PayPalError("PayPal is down.")

    gateway.create_invoice = refuse
    assert client.post(f"/api/scheduled/{item.id}/approve").status_code == 502
    assert items(client)[0].status == "PENDING"
    assert items(client)[0].invoice_id is None
    with Session(client.app.state.engine) as session:
        assert instalment_invoices(session) == []

    gateway.create_invoice = real_create
    assert client.post(f"/api/scheduled/{item.id}/approve").status_code == 200
    assert len(gateway.invoices) == 1


def test_the_form_deletes_its_draft_when_paypal_refuses_to_create_it(client):
    def refuse(**facts):
        raise PayPalError("PayPal is down.")

    client.app.state.gateway.create_invoice = refuse
    form = plan_form(client)
    with Session(client.app.state.engine) as session:
        before = len(session.exec(Invoice.__table__.select()).all())
    client.post("/invoices", data=form, follow_redirects=False)
    with Session(client.app.state.engine) as session:
        assert len(session.exec(Invoice.__table__.select()).all()) == before
