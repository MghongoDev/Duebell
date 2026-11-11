"""A sandbox call whose answer was lost counts as possibly done (issue #113).

The tests drive SandboxPayPal through httpx.MockTransport, so nothing leaves
the machine.
"""

from datetime import timedelta
from decimal import Decimal

import httpx
import pytest
from sqlmodel import Session

from app.config import Settings
from app.models import InvoiceStatus, ReminderStatus, User, init_db, make_engine, utcnow
from app.paypal import MockPayPal, PayPalError, PayPalTimeout, SandboxPayPal
from app.reminders import draft_reminders, send_reminder
from app.services import SEND_UNCONFIRMED, InvoiceNotSent, create_invoice, send_unsent_invoice
from app.validation import InvoiceProposal

SETTINGS = Settings(database_url="sqlite://", mock_webhooks=True)
SANDBOX = Settings(
    database_url="sqlite://",
    paypal_mode="sandbox",
    paypal_client_id="client-id",
    paypal_client_secret="client-secret",
    paypal_base_url="https://api.test",
)


class FakePayPal:
    """Answers the token call; fails the chosen action the way it is told to."""

    def __init__(self, action: str, failure):
        self.action = action
        self.failure = failure
        self.calls: list[httpx.Request] = []
        self.failing = True

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/oauth2/token":
            return httpx.Response(200, json={"access_token": "token", "expires_in": 3600})
        if request.url.path.endswith(self.action):
            self.calls.append(request)
            if self.failing:
                raise self.failure(request)
        return httpx.Response(204)


def read_timeout(request):
    # PayPal received the call and may have acted; the answer is lost.
    return httpx.ReadTimeout("read timed out", request=request)


def connect_error(request):
    # The connection never opened, so PayPal never saw the call.
    return httpx.ConnectError("connection refused", request=request)


def sandbox(fake: FakePayPal) -> SandboxPayPal:
    client = httpx.Client(base_url="https://api.test", transport=httpx.MockTransport(fake))
    return SandboxPayPal(SANDBOX, client=client, retry_delay=0)


@pytest.fixture
def session():
    engine = make_engine("sqlite://")
    init_db(engine)
    with Session(engine) as session:
        yield session


def sent_invoice(session):
    user = User(email="owner@duebell.test", password_hash="x")
    session.add(user)
    session.commit()
    proposal = InvoiceProposal(
        client_name="Acme Studio",
        client_email="billing@acme.test",
        description="Logo design",
        amount=Decimal("1200.00"),
        currency="USD",
        terms_days=14,
    )
    invoice = create_invoice(session, proposal, SETTINGS, MockPayPal(), user_id=user.id)
    return user, invoice


def pending_reminder(session):
    user, invoice = sent_invoice(session)
    invoice.due_at = utcnow() - timedelta(days=3)
    session.add(invoice)
    session.commit()
    return draft_reminders(session, SETTINGS, scope=user.id)[0]


def unsent_invoice(session):
    _, invoice = sent_invoice(session)
    invoice.status = InvoiceStatus.DRAFT
    session.add(invoice)
    session.commit()
    return invoice


def test_a_timed_out_post_raises_the_timeout_error():
    fake = FakePayPal("/remind", read_timeout)
    with pytest.raises(PayPalTimeout):
        sandbox(fake).send_reminder("INV-1", "Subject", "Note")
    assert len(fake.calls) == 3


def test_a_connect_error_stays_a_plain_error():
    fake = FakePayPal("/remind", connect_error)
    with pytest.raises(PayPalError) as caught:
        sandbox(fake).send_reminder("INV-1", "Subject", "Note")
    assert not isinstance(caught.value, PayPalTimeout)


def test_a_timed_out_read_stays_a_plain_error():
    fake = FakePayPal("/invoices/INV-1", read_timeout)
    with pytest.raises(PayPalError) as caught:
        sandbox(fake).get_invoice("INV-1")
    assert not isinstance(caught.value, PayPalTimeout)


def test_a_timed_out_token_fetch_stays_a_plain_error():
    def handler(request):
        raise httpx.ReadTimeout("read timed out", request=request)

    client = httpx.Client(base_url="https://api.test", transport=httpx.MockTransport(handler))
    gateway = SandboxPayPal(SANDBOX, client=client, retry_delay=0)
    with pytest.raises(PayPalError) as caught:
        gateway.send_reminder("INV-1", "Subject", "Note")
    assert not isinstance(caught.value, PayPalTimeout)


def test_a_timed_out_reminder_counts_as_sent_and_is_not_sent_again(session):
    reminder = pending_reminder(session)
    fake = FakePayPal("/remind", read_timeout)
    gateway = sandbox(fake)

    outcome = send_reminder(session, reminder, gateway, None, ("paypal",))

    assert outcome.reminder.status == ReminderStatus.SENT
    assert outcome.reminder.sent_channels == "paypal"
    assert any("may still send it" in warning for warning in outcome.warnings)
    calls = len(fake.calls)
    fake.failing = False
    with pytest.raises(ValueError):
        send_reminder(session, outcome.reminder, gateway, None, ("paypal",))
    assert len(fake.calls) == calls


def test_a_reminder_whose_connection_failed_can_be_retried(session):
    reminder = pending_reminder(session)
    fake = FakePayPal("/remind", connect_error)
    gateway = sandbox(fake)

    with pytest.raises(PayPalError):
        send_reminder(session, reminder, gateway, None, ("paypal",))
    session.refresh(reminder)
    assert reminder.status == ReminderStatus.PENDING
    assert reminder.paypal_attempted_at is None

    fake.failing = False
    outcome = send_reminder(session, reminder, gateway, None, ("paypal",))
    assert outcome.reminder.status == ReminderStatus.SENT
    assert outcome.warnings == []


def test_a_timed_out_invoice_send_counts_as_sent(session):
    invoice = unsent_invoice(session)
    gateway = sandbox(FakePayPal("/send", read_timeout))
    warnings: list[str] = []

    sent = send_unsent_invoice(session, invoice, gateway, warnings=warnings)

    assert sent.status == InvoiceStatus.SENT
    assert warnings == [SEND_UNCONFIRMED]


def test_an_invoice_send_whose_connection_failed_can_be_retried(session):
    invoice = unsent_invoice(session)
    fake = FakePayPal("/send", connect_error)
    gateway = sandbox(fake)

    with pytest.raises(InvoiceNotSent):
        send_unsent_invoice(session, invoice, gateway, warnings=[])
    session.refresh(invoice)
    assert invoice.status == InvoiceStatus.DRAFT

    fake.failing = False
    warnings: list[str] = []
    sent = send_unsent_invoice(session, invoice, gateway, warnings=warnings)
    assert sent.status == InvoiceStatus.SENT
    assert warnings == []


def answers(*outcomes):
    """A transport that gives each action call the next outcome in turn."""
    calls = []

    def handler(request):
        if request.url.path == "/v1/oauth2/token":
            return httpx.Response(200, json={"access_token": "token", "expires_in": 3600})
        outcome = outcomes[len(calls)]
        calls.append(request)
        if isinstance(outcome, int):
            return httpx.Response(outcome, json={"name": "INVOICE_ALREADY_SENT"})
        raise outcome(request)

    client = httpx.Client(base_url="https://api.test", transport=httpx.MockTransport(handler))
    return SandboxPayPal(SANDBOX, client=client, retry_delay=0)


def test_a_refusal_after_a_lost_answer_may_mean_paypal_acted():
    gateway = answers(read_timeout, 422)
    with pytest.raises(PayPalTimeout):
        gateway.send_invoice("INV-1")


def test_a_refusal_alone_stays_a_plain_error():
    gateway = answers(connect_error, 422)
    with pytest.raises(PayPalError) as caught:
        gateway.send_invoice("INV-1")
    assert not isinstance(caught.value, PayPalTimeout)
