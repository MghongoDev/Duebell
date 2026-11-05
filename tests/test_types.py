"""Status, kind and risk-level types keep the stored values (issue #31)."""

from datetime import timedelta

from sqlalchemy import text
from sqlmodel import Session

from app.models import InvoiceStatus, ReminderStatus, ScheduledInvoiceStatus, ScheduleKind, utcnow
from app.risk import RiskLevel
from tests.conftest import add_late_payer

FORM = {
    "client_name": "Acme Studio",
    "client_email": "billing@acme.test",
    "description": "Logo design",
    "amount": "1200.00",
    "currency": "USD",
    "terms_days": "2",
}


def raw(client, sql):
    with Session(client.app.state.engine) as session:
        return session.execute(text(sql)).all()


def test_the_database_holds_plain_strings(client):
    client.post("/invoices", data=dict(FORM, **add_late_payer(client), instalments="2"), follow_redirects=False)
    client.post("/invoices", data=dict(FORM, repeat="MONTH"), follow_redirects=False)
    client.post("/api/reminders/scan")

    statuses = {row[0] for row in raw(client, "SELECT status FROM invoice")}
    assert statuses == {"PAID", "SENT"}
    assert {row[0] for row in raw(client, "SELECT kind FROM schedule")} == {"INSTALMENT", "RECURRING"}
    assert {row[0] for row in raw(client, "SELECT status FROM scheduledinvoice")} == {"PENDING", "UPCOMING"}
    assert {row[0] for row in raw(client, "SELECT risk_level FROM invoice WHERE risk_level IS NOT NULL")} <= {
        "LOW",
        "MEDIUM",
        "HIGH",
    }


def test_rows_written_as_strings_still_match_the_types(client):
    """A database from before the types holds bare strings. They must still compare equal."""
    due = (utcnow() + timedelta(days=1)).isoformat(sep=" ")
    with Session(client.app.state.engine) as session:
        session.execute(
            text(
                "INSERT INTO client (user_id, name, email, created_at) "
                "VALUES (:user, 'Old Client', 'old@example.test', :now)"
            ),
            {"user": client.user_id, "now": due},
        )
        client_id = session.execute(text("SELECT id FROM client WHERE email = 'old@example.test'")).scalar_one()
        session.execute(
            text(
                "INSERT INTO invoice (user_id, client_id, invoice_number, paypal_invoice_id, description, "
                "amount_cents, currency, terms_days, issued_at, due_at, status, risk_level, risk_reasons, "
                "limited_data, created_at) VALUES (:user, :client, 'OLD-1', 'OLD-PAYPAL-1', 'Old work', 50000, "
                "'USD', 14, :now, :due, 'SENT', 'HIGH', '', 0, :now)"
            ),
            {"user": client.user_id, "client": client_id, "now": due, "due": due},
        )
        session.commit()

    assert client.post("/api/reminders/scan").json()["created"] == 1
    [reminder] = client.get("/api/reminders").json()
    assert reminder["status"] == ReminderStatus.PENDING == "PENDING"
    assert reminder["tone"] == "firm", "a stored 'HIGH' must still read as RiskLevel.HIGH"


def test_types_equal_their_stored_strings():
    assert InvoiceStatus.MARKED_AS_PAID == "MARKED_AS_PAID"
    assert ScheduleKind.INSTALMENT == "INSTALMENT"
    assert ScheduledInvoiceStatus.UPCOMING == "UPCOMING"
    assert RiskLevel("HIGH") is RiskLevel.HIGH
