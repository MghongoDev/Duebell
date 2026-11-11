"""Duebell hands out each invoice number once, even after deleting a draft (issue #114)."""

from sqlalchemy import text
from sqlmodel import Session, select

from app.config import Settings
from app.models import Client, Invoice, init_db, make_engine, utcnow
from app.paypal import MockPayPal, PayPalError
from app.services import create_invoice, draft_invoice
from app.validation import InvoiceProposal

PROPOSAL = InvoiceProposal(
    client_name="Acme Studio",
    client_email="billing@acme.test",
    description="Logo",
    amount="100.00",
    currency="USD",
    terms_days=14,
)


class HeldNumbers(MockPayPal):
    """Refuses a number it already holds, as PayPal does. Loses its first answer."""

    def __init__(self):
        super().__init__()
        self.numbers = set()
        self.lose_answer = True

    def create_invoice(self, **facts):
        if facts["invoice_number"] in self.numbers:
            raise PayPalError("DUPLICATE_INVOICE_NUMBER")
        self.numbers.add(facts["invoice_number"])
        paypal_id = super().create_invoice(**facts)
        if self.lose_answer:
            self.lose_answer = False
            raise PayPalError("PayPal could not be reached after retries: read timed out")
        return paypal_id


def _engine():
    engine = make_engine("sqlite://")
    init_db(engine)
    return engine


def _settings():
    return Settings(database_url="sqlite://", amount_cap_cents=1_000_000, allowed_currencies=("USD",))


def test_a_create_paypal_may_have_made_does_not_block_the_next_invoice():
    engine, gateway = _engine(), HeldNumbers()
    with Session(engine) as session:
        try:
            create_invoice(session, PROPOSAL, _settings(), gateway, user_id=None)
        except PayPalError:
            pass
        else:
            raise AssertionError("the first create should fail")
        assert session.exec(select(Invoice)).all() == [], "the failed draft is deleted"
        [held] = gateway.numbers

        invoice = create_invoice(session, PROPOSAL, _settings(), gateway, user_id=None)

    assert invoice.invoice_number != held
    assert invoice.status == "SENT"


def test_a_deleted_draft_number_is_not_given_out_again():
    engine = _engine()
    with Session(engine) as session:
        first = draft_invoice(session, PROPOSAL, user_id=None)
        number = first.invoice_number
        session.delete(first)
        session.commit()

        second = draft_invoice(session, PROPOSAL, user_id=None)

    assert second.invoice_number != number


def test_an_older_database_numbers_above_every_existing_invoice():
    engine = _engine()
    today = f"{utcnow():%Y%m%d}"
    with Session(engine) as session:
        session.add(Client(id=1, name="Acme", email="ap@acme.test"))
        session.add(Invoice(id=1, client_id=1, invoice_number=f"DUE-{today}-0042", amount_cents=100))
        session.add(Invoice(id=2, client_id=1, invoice_number="DUE-SEED-0007", amount_cents=100))
        session.commit()
    # A database from a build before the counter.
    with engine.begin() as connection:
        connection.execute(text("DROP TABLE IF EXISTS invoicenumbercounter"))
    init_db(engine)

    with Session(engine) as session:
        invoice = draft_invoice(session, PROPOSAL, user_id=None)
        numbers = sorted(row.invoice_number for row in session.exec(select(Invoice)).all())

    assert invoice.invoice_number == f"DUE-{today}-0043"
    assert numbers == sorted([f"DUE-{today}-0042", "DUE-SEED-0007", f"DUE-{today}-0043"])


def test_the_counter_stays_above_rows_added_without_it():
    engine = _engine()
    today = f"{utcnow():%Y%m%d}"
    with Session(engine) as session:
        draft_invoice(session, PROPOSAL, user_id=None)
        session.add(Invoice(id=50, client_id=1, invoice_number="IMPORTED-1", amount_cents=100))
        session.commit()

        invoice = draft_invoice(session, PROPOSAL, user_id=None)

    assert invoice.invoice_number == f"DUE-{today}-0051"
