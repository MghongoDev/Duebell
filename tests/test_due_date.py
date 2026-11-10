"""PayPal gets the due date Duebell stores (issue #35).

Each gateway must send the stored due date itself, not a rounded NET term,
so reminders and the overdue flag match what the client sees in PayPal.
"""

from decimal import Decimal

import pytest
from sqlmodel import Session

from app.config import Settings
from app.models import init_db, make_engine
from app.paypal import MockPayPal, SandboxPayPal, ToolkitPayPal
from app.services import create_invoice
from app.validation import InvoiceProposal
from tests.test_paypal_sandbox import FakeClient
from tests.test_paypal_sandbox import settings as sandbox_settings
from tests.test_toolkit import FakeToolkit
from tests.test_toolkit import settings as toolkit_settings


def sent_payment_term(fake):
    """The payment term in the create call. Create is followed by a send call."""
    (body,) = [kwargs["json"] for method, url, kwargs in fake.requests if url == "/v2/invoicing/invoices"]
    return body["detail"]["payment_term"]


def mock_gateway():
    gateway = MockPayPal()
    return gateway, lambda paypal_id: gateway.invoices[paypal_id]["payment_term"]


def sandbox_gateway():
    fake = FakeClient()
    return SandboxPayPal(sandbox_settings(), client=fake, retry_delay=0), lambda _: sent_payment_term(fake)


def toolkit_gateway():
    fake = FakeClient()
    toolkit = FakeToolkit()
    gateway = ToolkitPayPal(toolkit_settings(), toolkit=toolkit, client=fake, retry_delay=0)
    return gateway, lambda _: sent_payment_term(fake)


GATEWAYS = {"mock": mock_gateway, "sandbox": sandbox_gateway, "toolkit": toolkit_gateway}


def create(gateway, terms_days):
    settings = Settings(database_url="sqlite://", terms_min_days=0, terms_max_days=120)
    engine = make_engine(settings.database_url)
    init_db(engine)
    proposal = InvoiceProposal(
        client_name="Acme Studio",
        client_email="billing@acme.test",
        description="Logo design",
        amount=Decimal("1200.00"),
        currency="USD",
        terms_days=terms_days,
    )
    with Session(engine) as session:
        invoice = create_invoice(session, proposal, settings, gateway, user_id=None)
        return invoice.paypal_invoice_id, invoice.issued_at, invoice.due_at


@pytest.mark.parametrize("name", GATEWAYS)
@pytest.mark.parametrize("terms_days", [14, 16, 31, 120])
def test_the_due_date_sent_is_the_one_stored(name, terms_days):
    gateway, sent_term = GATEWAYS[name]()
    paypal_id, issued_at, due_at = create(gateway, terms_days)
    assert (due_at.date() - issued_at.date()).days == terms_days
    assert sent_term(paypal_id) == {"due_date": due_at.date().isoformat()}


@pytest.mark.parametrize("name", GATEWAYS)
def test_zero_days_is_due_on_receipt(name):
    gateway, sent_term = GATEWAYS[name]()
    paypal_id, issued_at, due_at = create(gateway, 0)
    assert due_at == issued_at
    assert sent_term(paypal_id) == {"term_type": "DUE_ON_RECEIPT"}
