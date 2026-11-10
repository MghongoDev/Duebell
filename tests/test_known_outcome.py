"""Risk counts only invoices whose outcome is known (issue #46)."""

from datetime import datetime, timedelta, timezone

from sqlmodel import Session, select

from app.learning import outcome, training_rows
from app.models import Client, Invoice, User, init_db, make_engine, utcnow
from app.ownership import EVERY_USER
from app.risk import HistoryInvoice, extract_features, score_invoice
from app.services import client_history, preview_risk
from tests.conftest import add_late_payer

NOW = datetime(2026, 11, 10, 12, 0, tzinfo=timezone.utc)


def paid(days_late: int) -> HistoryInvoice:
    due = NOW - timedelta(days=60)
    return HistoryInvoice(amount_cents=100_000, due_at=due, paid_at=due + timedelta(days=days_late))


def open_not_due() -> HistoryInvoice:
    return HistoryInvoice(amount_cents=100_000, due_at=NOW + timedelta(days=10))


def test_open_invoices_do_not_pull_a_late_payer_down():
    late = [paid(25)] * 4
    alone = score_invoice(100_000, 14, late, now=NOW)
    with_open = score_invoice(100_000, 14, late + [open_not_due()] * 4, now=NOW)
    assert alone.score == 0.92
    assert with_open.score == alone.score
    assert with_open.reasons == alone.reasons


def test_only_open_invoices_is_limited_data_and_says_why():
    result = score_invoice(100_000, 14, [open_not_due(), open_not_due()], now=NOW)
    assert result.limited_data is True
    assert result.score == 0.40
    assert not any("on time" in reason for reason in result.reasons)
    assert result.reasons[0] == (
        "No paid or overdue invoices yet. 2 open invoices are not yet due, so this score is a rough guide."
    )
    assert result.limited_data_reason == result.reasons[0]


def test_an_invoice_less_than_a_day_overdue_is_not_yet_known():
    just_due = HistoryInvoice(amount_cents=100_000, due_at=NOW - timedelta(hours=3))
    assert extract_features(100_000, 14, [just_due], now=NOW)["prior_count"] == 0
    invoice = Invoice(client_id=1, invoice_number="J", amount_cents=1, status="SENT", due_at=NOW - timedelta(hours=3))
    assert outcome(invoice, NOW) is None


def test_limited_history_reason_counts_only_known_outcomes():
    result = score_invoice(100_000, 14, [paid(0), open_not_due()], now=NOW)
    assert result.limited_data is True
    assert result.limited_data_reason == (
        "Limited history. Two or more paid or overdue invoices would sharpen this score."
    )


def test_client_with_a_draft_and_an_open_invoice_gets_limited_data():
    db = make_engine("sqlite://")
    init_db(db)
    now = utcnow()
    with Session(db) as session:
        owner = User(email="o@duebell.test", password_hash="x")
        session.add(owner)
        session.commit()
        client = Client(name="Acme", email="a@example.test", user_id=owner.id)
        session.add(client)
        session.commit()
        for number, status, due in (("D", "DRAFT", now - timedelta(days=5)), ("S", "SENT", now + timedelta(days=9))):
            session.add(Invoice(user_id=owner.id, client_id=client.id, invoice_number=number, amount_cents=100_000,
                                status=status, issued_at=now - timedelta(days=20), due_at=due))
        session.commit()
        result = preview_risk(session, owner.id, email="a@example.test", amount_cents=100_000,
                              terms_days=14, currency="USD")
    assert result.limited_data is True
    assert result.level == "MEDIUM"
    assert not any("Paid all" in reason for reason in result.reasons)


def test_live_score_and_training_rows_use_the_same_history():
    db = make_engine("sqlite://")
    init_db(db)
    now = utcnow()
    as_of = now - timedelta(days=10)
    with Session(db) as session:
        client = Client(name="Acme", email="a@example.test")
        session.add(client)
        session.commit()
        earlier = now - timedelta(days=90)
        mix = [
            ("late", "PAID", earlier + timedelta(days=30), earlier + timedelta(days=45)),
            ("on-time", "PAID", earlier + timedelta(days=30), earlier + timedelta(days=29)),
            ("draft", "DRAFT", earlier + timedelta(days=30), None),
            ("overdue", "SENT", as_of - timedelta(days=4), None),
            ("open", "SENT", as_of + timedelta(days=20), None),
            ("paid-later", "PAID", as_of + timedelta(days=5), as_of + timedelta(days=3)),
        ]
        for number, status, due, paid_at in mix:
            session.add(Invoice(client_id=client.id, invoice_number=number, amount_cents=100_000, status=status,
                                terms_days=30, issued_at=earlier, due_at=due, paid_at=paid_at))
        session.commit()
        session.add(Invoice(client_id=client.id, invoice_number="target", amount_cents=100_000, status="PAID",
                            terms_days=14, issued_at=as_of, due_at=as_of + timedelta(days=5),
                            paid_at=as_of + timedelta(days=5)))
        session.commit()
        live = extract_features(100_000, 14, client_history(session, client.id, as_of), now=as_of, currency="USD")
        # Only the target has net 14 terms, which picks out its row.
        [row] = [row for row in training_rows(session, now, scope=EVERY_USER) if row["terms_days"] == 14]
    # late, on-time and overdue count; the draft, the open one and the one paid later do not.
    assert live["prior_count"] == 3
    assert live["late_count"] == 2
    for name in ("late_share", "average_days_late", "amount_ratio", "new_client"):
        assert row[name] == float(live[name])


def test_dashboard_shows_the_servers_limited_data_reason(client):
    client.post("/invoices", data={
        "client_name": "Acme Studio", "client_email": "billing@acme.test", "description": "Logo",
        "amount": "1200.00", "currency": "USD", "terms_days": "14",
    })
    row = client.get("/api/invoices").json()[0]
    assert row["limited_data"] is True
    assert row["limited_data_reason"].startswith("New client.")
    # The dashboard's script is a static file since #49.
    script = client.get("/static/dashboard.js").text
    assert "no history yet" not in client.get("/").text + script
    assert "escapeHtml(limitedReason)" in script


def test_late_payer_with_open_invoices_still_scores_high(client):
    form = add_late_payer(client)
    with Session(client.app.state.engine) as session:
        late_payer = session.exec(
            select(Client).where(Client.email == form["client_email"])
        ).one()
        for index in range(4):
            session.add(Invoice(user_id=client.user_id, client_id=late_payer.id, invoice_number=f"OPEN-{index}",
                                amount_cents=100_000, status="SENT", issued_at=utcnow() - timedelta(days=2),
                                due_at=utcnow() + timedelta(days=12)))
        session.commit()
        result = preview_risk(session, client.user_id, email=form["client_email"], amount_cents=100_000,
                              terms_days=14, currency="USD")
    assert result.score == 0.92
    assert result.reasons[0].startswith("Paid 4 of last 4 invoices late")
