"""Second-currency invoices (issue #23)."""

from dataclasses import replace
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

from app.models import make_engine, utcnow
from app.risk import HistoryInvoice, score_invoice
from app.services import dashboard_totals
from app.web import create_app
from tests.conftest import sign_in

FORM = {
    "client_name": "Acme Studio",
    "client_email": "billing@acme.test",
    "description": "Logo design",
    "amount": "1000.00",
    "currency": "USD",
    "terms_days": "14",
}


@pytest.fixture
def client(settings):
    settings = replace(settings, allowed_currencies=("USD", "EUR"))
    return sign_in(TestClient(create_app(settings=settings, engine=make_engine(settings.database_url))))


def paid_on_time(amount_cents, currency):
    due = utcnow() - timedelta(days=30)
    return HistoryInvoice(amount_cents=amount_cents, due_at=due, paid_at=due, currency=currency)


def test_amount_ratio_ignores_history_in_other_currencies():
    history = [paid_on_time(100_000, "USD"), paid_on_time(100_000, "USD"), paid_on_time(900_000, "EUR")]
    usd = score_invoice(110_000, 14, history, currency="USD")
    assert not any("usual amount" in reason for reason in usd.reasons)
    big_usd = score_invoice(200_000, 14, history, currency="USD")
    assert any("2.0x the client's usual amount" in reason for reason in big_usd.reasons)


def test_first_invoice_in_a_new_currency_says_so():
    history = [paid_on_time(100_000, "USD"), paid_on_time(100_000, "USD")]
    result = score_invoice(500_000, 14, history, currency="EUR")
    assert any("First EUR invoice" in reason for reason in result.reasons)
    assert not any("x the client's usual amount" in reason for reason in result.reasons)


def test_totals_never_add_currencies_together():
    rows = [
        {"status": "SENT", "paypal_invoice_id": "A", "currency": "USD", "amount_cents": 100_000, "overdue": True},
        {"status": "SENT", "paypal_invoice_id": "B", "currency": "EUR", "amount_cents": 50_000, "overdue": True},
        {"status": "PAID", "paypal_invoice_id": "C", "currency": "EUR", "amount_cents": 20_000, "overdue": False},
    ]
    totals = dashboard_totals(rows)
    assert totals["outstanding"] == {"EUR": 50_000, "USD": 100_000}
    assert totals["outstanding_display"] == "500.00 EUR · 1,000.00 USD"
    assert totals["paid"] == {"EUR": 20_000}
    assert totals["overdue_count"] == 2
    assert totals["overdue_by_currency"] == {"EUR": 1, "USD": 1}


def test_eur_invoice_reaches_paypal_in_eur(client):
    response = client.post("/invoices", data=dict(FORM, currency="EUR"), follow_redirects=False)
    assert response.status_code == 303
    sent = next(iter(client.app.state.gateway.invoices.values()))
    assert sent["currency"] == "EUR"
    row = client.get("/api/invoices").json()[0]
    assert row["currency"] == "EUR"


def test_mixed_client_gets_per_currency_totals_and_risk(client):
    client.post("/invoices", data=FORM, follow_redirects=False)
    client.post("/invoices", data=dict(FORM, amount="5000.00", currency="EUR"), follow_redirects=False)
    rows = client.get("/api/invoices").json()
    eur = next(row for row in rows if row["currency"] == "EUR")
    assert any("First EUR invoice" in reason for reason in eur["risk_reasons"])
    page = client.get("/").text
    assert "1,000.00 USD" in page and "5,000.00 EUR" in page
    assert '<option value="EUR"' in page


def test_currency_off_the_list_is_still_rejected(client):
    response = client.post("/invoices", data=dict(FORM, currency="GBP"))
    assert response.status_code == 422
    assert "Currency must be one of: USD, EUR" in response.text


def test_marked_as_paid_is_collected_and_never_overdue(client):
    from sqlmodel import Session

    from app.models import Invoice

    client.post("/invoices", data=FORM, follow_redirects=False)
    with Session(client.app.state.engine) as session:
        invoice = session.exec(__import__("sqlmodel").select(Invoice)).one()
        invoice.status = "MARKED_AS_PAID"
        invoice.paid_at = utcnow()
        invoice.due_at = utcnow() - timedelta(days=5)
        session.add(invoice)
        session.commit()
    [row] = client.get("/api/invoices").json()
    assert row["overdue"] is False
    assert row["closed"] is True, "the grid hides Simulate payment for closed invoices"
    totals = dashboard_totals([row])
    assert totals["paid"] == {"USD": 100_000}
    assert totals["overdue_count"] == 0


def test_first_invoice_in_a_new_currency_is_flagged_limited_data():
    history = [paid_on_time(100_000, "USD"), paid_on_time(100_000, "USD"), paid_on_time(100_000, "USD")]
    assert score_invoice(100_000, 14, history, currency="USD").limited_data is False
    assert score_invoice(100_000, 14, history, currency="EUR").limited_data is True
