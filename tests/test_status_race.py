"""A status saved during a PayPal call is never overwritten by what the call read before it (issue #115).

Each test commits a webhook from inside the fake PayPal call, from a second
session, as a webhook request landing mid-call would.
"""

import json
from decimal import Decimal

import pytest
from sqlmodel import Session, select

from app.models import Invoice, InvoiceStatus, Reminder, ReminderStatus, User, as_utc, init_db, make_engine, utcnow
from app.paypal import MockPayPal, PayPalTimeout
from app.services import (
    draft_invoice,
    handle_webhook,
    refresh_invoice,
    send_unsent_invoice,
    simulate_payment,
)
from app.validation import InvoiceProposal


@pytest.fixture
def engine(tmp_path):
    engine = make_engine(f"sqlite:///{tmp_path / 'race.db'}")
    init_db(engine)
    return engine


@pytest.fixture
def gateway():
    return MockPayPal(accepts_webhooks=True)


def held_invoice(engine, gateway, status=InvoiceStatus.DRAFT) -> int:
    """A saved invoice PayPal holds, in the given status. Returns its id."""
    with Session(engine) as session:
        user = User(email="owner@duebell.test", password_hash="x")
        session.add(user)
        session.commit()
        proposal = InvoiceProposal(
            client_name="Acme",
            client_email="ap@acme.test",
            description="Work",
            amount=Decimal("100"),
            currency="USD",
            terms_days=14,
        )
        invoice = draft_invoice(session, proposal, user_id=user.id)
        invoice.paypal_invoice_id = gateway.create_invoice(
            invoice_number=invoice.invoice_number,
            amount_cents=invoice.amount_cents,
            currency="USD",
            description="Work",
            terms_days=14,
            client_name="Acme",
            client_email="ap@acme.test",
            issued_at=invoice.issued_at,
            due_at=invoice.due_at,
        )
        invoice.status = status
        session.add(invoice)
        session.commit()
        return invoice.id


def land_webhook(engine, gateway, paypal_id, event_type, status=None, event_id="WH-RACE"):
    """Commit a webhook from another session, as a request arriving mid-call would."""
    resource = {"id": paypal_id, **({"status": status} if status else {})}
    body = json.dumps({"id": event_id, "event_type": event_type, "resource": resource})
    with Session(engine) as other:
        result = handle_webhook(other, gateway, {}, body)
    assert result["applied"] is True


def stored(engine, invoice_id) -> Invoice:
    with Session(engine) as session:
        invoice = session.get(Invoice, invoice_id)
        session.expunge(invoice)
        return invoice


def test_a_paid_webhook_during_a_send_keeps_the_invoice_paid(engine, gateway):
    invoice_id = held_invoice(engine, gateway)
    original_send = gateway.send_invoice

    def send_then_paid(paypal_id):
        original_send(paypal_id)
        land_webhook(engine, gateway, paypal_id, "INVOICING.INVOICE.PAID")

    gateway.send_invoice = send_then_paid
    with Session(engine) as session:
        invoice = session.get(Invoice, invoice_id)
        sent = send_unsent_invoice(session, invoice, gateway, warnings=[])
        assert sent.status == InvoiceStatus.PAID

    invoice = stored(engine, invoice_id)
    assert invoice.status == InvoiceStatus.PAID
    assert invoice.paid_at is not None


def test_a_timed_out_send_gives_no_warning_once_a_webhook_closed_the_invoice(engine, gateway):
    invoice_id = held_invoice(engine, gateway)

    def paid_then_timeout(paypal_id):
        land_webhook(engine, gateway, paypal_id, "INVOICING.INVOICE.PAID")
        raise PayPalTimeout("read timed out")

    gateway.send_invoice = paid_then_timeout
    warnings: list[str] = []
    with Session(engine) as session:
        send_unsent_invoice(session, session.get(Invoice, invoice_id), gateway, warnings=warnings)

    assert stored(engine, invoice_id).status == InvoiceStatus.PAID
    assert warnings == []


def test_a_send_with_no_webhook_still_marks_the_invoice_sent(engine, gateway):
    invoice_id = held_invoice(engine, gateway)
    with Session(engine) as session:
        sent = send_unsent_invoice(session, session.get(Invoice, invoice_id), gateway, warnings=[])
        assert sent.status == InvoiceStatus.SENT
    assert stored(engine, invoice_id).status == InvoiceStatus.SENT


def test_a_paid_webhook_during_a_refresh_is_not_undone_by_its_older_answer(engine, gateway):
    invoice_id = held_invoice(engine, gateway, InvoiceStatus.SENT)
    with Session(engine) as session:
        session.add(
            Reminder(invoice_id=invoice_id, user_id=session.get(Invoice, invoice_id).user_id, draft_text="Reminder")
        )
        session.commit()

    def partly_paid_then_paid(paypal_id):
        # PayPal answered before the full payment the webhook then reports.
        land_webhook(engine, gateway, paypal_id, "INVOICING.INVOICE.PAID")
        return InvoiceStatus.PARTIALLY_PAID

    gateway.get_invoice = partly_paid_then_paid
    with Session(engine) as session:
        refreshed = refresh_invoice(session, session.get(Invoice, invoice_id), gateway)
        assert refreshed.status == InvoiceStatus.PAID

    invoice = stored(engine, invoice_id)
    assert invoice.status == InvoiceStatus.PAID
    assert invoice.paid_at is not None
    with Session(engine) as session:
        reminder = session.exec(select(Reminder).where(Reminder.invoice_id == invoice_id)).one()
        assert reminder.status == ReminderStatus.REJECTED


def test_a_cancel_webhook_during_a_simulated_payment_keeps_the_invoice_cancelled(engine, gateway):
    invoice_id = held_invoice(engine, gateway, InvoiceStatus.SENT)
    original_record = gateway.record_payment

    def record_then_cancelled(paypal_id, amount_cents, currency):
        land_webhook(engine, gateway, paypal_id, "INVOICING.INVOICE.CANCELLED")
        original_record(paypal_id, amount_cents, currency)

    gateway.record_payment = record_then_cancelled
    with Session(engine) as session:
        result = simulate_payment(session, session.get(Invoice, invoice_id), gateway)
        assert result.status == InvoiceStatus.CANCELLED

    invoice = stored(engine, invoice_id)
    assert invoice.status == InvoiceStatus.CANCELLED
    assert invoice.paid_at is None


def test_a_simulated_payment_with_no_webhook_still_marks_the_invoice_paid(engine, gateway):
    invoice_id = held_invoice(engine, gateway, InvoiceStatus.SENT)
    before = utcnow()
    with Session(engine) as session:
        simulate_payment(session, session.get(Invoice, invoice_id), gateway)
    invoice = stored(engine, invoice_id)
    assert invoice.status == InvoiceStatus.PAID
    assert as_utc(invoice.paid_at) >= before
