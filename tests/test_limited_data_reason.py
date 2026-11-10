"""An invoice stores its limited-data reason in its own field (issue #61)."""

from datetime import datetime, timedelta, timezone

from sqlalchemy import text
from sqlmodel import Session, select

from app import risk
from app.models import Client, Invoice, init_db, make_engine
from app.ownership import EVERY_USER
from app.services import invoice_rows

NOW = datetime(2026, 11, 10, 12, 0, tzinfo=timezone.utc)

FORM = {
    "client_name": "Acme Studio",
    "client_email": "billing@acme.test",
    "description": "Logo",
    "amount": "1200.00",
    "currency": "USD",
    "terms_days": "14",
}


def test_scoring_stores_the_limited_data_reason(client):
    client.post("/invoices", data=FORM)
    with Session(client.app.state.engine) as session:
        invoice = session.exec(select(Invoice)).one()
    assert invoice.limited_data is True
    assert invoice.limited_data_reason == invoice.risk_reasons.splitlines()[0]
    assert invoice.limited_data_reason.startswith("New client.")


def test_a_reworded_reason_still_reaches_the_dashboard(client, monkeypatch):
    monkeypatch.setattr(risk, "NEW_CLIENT", "Never billed this client before.")
    client.post("/invoices", data=FORM)
    row = client.get("/api/invoices").json()[0]
    assert row["limited_data"] is True
    assert row["limited_data_reason"] == (
        "Never billed this client before. No payment history yet, so this score is a rough guide."
    )


def test_only_a_limited_data_score_carries_a_reason():
    due = NOW - timedelta(days=60)
    paid = risk.HistoryInvoice(amount_cents=100_000, due_at=due, paid_at=due)
    assert risk.score_invoice(100_000, 14, [paid], now=NOW).limited_data_reason == (
        "Limited history. Two or more paid or overdue invoices would sharpen this score."
    )
    assert risk.score_invoice(100_000, 14, [paid, paid], now=NOW).limited_data_reason is None


def test_an_old_invoice_without_the_field_shows_limited_data_alone(tmp_path):
    url = f"sqlite:///{tmp_path / 'old.db'}"
    engine = make_engine(url)
    init_db(engine)
    with engine.begin() as connection:
        connection.execute(text('ALTER TABLE invoice DROP COLUMN "limited_data_reason"'))
        connection.execute(text("INSERT INTO client (name, email, created_at) VALUES ('Old', 'old@x.test', '2026-01-01')"))
        connection.execute(
            text(
                "INSERT INTO invoice (client_id, invoice_number, description, amount_cents, currency, terms_days, "
                "issued_at, due_at, status, risk_reasons, limited_data, created_at) VALUES (1, 'OLD-1', '', 50000, "
                "'USD', 14, '2026-01-01', '2026-01-15', 'SENT', "
                "'New client. No payment history yet, so this score is a rough guide.', 1, '2026-01-01')"
            )
        )
    engine.dispose()

    engine = make_engine(url)
    init_db(engine)
    with Session(engine) as session:
        assert session.exec(select(Client)).one().name == "Old"
        [row] = invoice_rows(session, EVERY_USER)
    assert row["limited_data"] is True
    assert row["limited_data_reason"] is None
