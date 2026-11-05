"""Recurring invoices and payment plans (issue #25)."""

from datetime import datetime, timedelta, timezone

import pytest
from sqlmodel import Session, select

from app.models import Invoice, Schedule, ScheduledInvoice, utcnow
from app.schedules import add_interval, split_amount

FORM = {
    "client_name": "Acme Studio",
    "client_email": "billing@acme.test",
    "description": "Monthly retainer",
    "amount": "1000.00",
    "currency": "USD",
    "terms_days": "14",
}


def items(client, **where):
    with Session(client.app.state.engine) as session:
        query = select(ScheduledInvoice).order_by(ScheduledInvoice.id)
        for key, value in where.items():
            query = query.where(getattr(ScheduledInvoice, key) == value)
        return session.exec(query).all()


def move_due(client, item_id, days_ago=1):
    with Session(client.app.state.engine) as session:
        item = session.get(ScheduledInvoice, item_id)
        item.issue_on = utcnow() - timedelta(days=days_ago)
        session.add(item)
        session.commit()


def test_split_is_exact_and_bounded():
    assert split_amount(100_000, 3) == [33_334, 33_333, 33_333]
    assert sum(split_amount(99_999, 6)) == 99_999
    with pytest.raises(ValueError):
        split_amount(100_000, 1)
    with pytest.raises(ValueError):
        split_amount(100_000, 7)


def test_monthly_interval_clamps_to_month_end():
    jan31 = datetime(2026, 1, 31, tzinfo=timezone.utc)
    assert add_interval(jan31, "MONTH", 1) == datetime(2026, 2, 28, tzinfo=timezone.utc)
    assert add_interval(jan31, "MONTH", 12) == datetime(2027, 1, 31, tzinfo=timezone.utc)
    assert add_interval(jan31, "WEEK", 2) == datetime(2026, 2, 14, tzinfo=timezone.utc)


def test_repeat_sends_now_and_queues_the_next_for_approval(client):
    response = client.post("/invoices", data=dict(FORM, repeat="MONTH", repeat_cycles="2"), follow_redirects=False)
    assert response.status_code == 303
    assert len(client.get("/api/invoices").json()) == 1
    [upcoming] = items(client)
    assert upcoming.status == "UPCOMING"

    # Not due yet: the scan leaves it alone.
    assert client.post("/api/reminders/scan").json()["scheduled"] == 0

    move_due(client, upcoming.id)
    assert client.post("/api/reminders/scan").json()["scheduled"] == 1
    assert len(client.app.state.gateway.invoices) == 1, "drafting must not reach PayPal"

    approved = client.post(f"/api/scheduled/{upcoming.id}/approve").json()
    assert approved["status"] == "SENT"
    assert len(client.app.state.gateway.invoices) == 2
    rows = client.get("/api/invoices").json()
    assert len(rows) == 2 and all(row["status"] == "SENT" for row in rows)


def test_repeat_stops_after_its_cycles(client):
    client.post("/invoices", data=dict(FORM, repeat="WEEK", repeat_cycles="2"), follow_redirects=False)
    for _ in range(2):
        [upcoming] = items(client, status="UPCOMING")
        move_due(client, upcoming.id)
        client.post("/api/reminders/scan")
    assert items(client, status="UPCOMING") == []
    assert len(items(client, status="PENDING")) == 2
    with Session(client.app.state.engine) as session:
        assert session.exec(select(Schedule)).one().status == "DONE"


def test_rejected_occurrence_skips_one_and_keeps_repeating(client):
    client.post("/invoices", data=dict(FORM, repeat="MONTH"), follow_redirects=False)
    [first] = items(client)
    move_due(client, first.id)
    client.post("/api/reminders/scan")
    assert client.post(f"/api/scheduled/{first.id}/reject").json()["status"] == "REJECTED"
    assert len(items(client, status="UPCOMING")) == 1
    assert len(client.app.state.gateway.invoices) == 1


def test_cancel_stops_future_drafts_and_leaves_sent_invoices(client):
    client.post("/invoices", data=dict(FORM, repeat="MONTH"), follow_redirects=False)
    with Session(client.app.state.engine) as session:
        schedule_id = session.exec(select(Schedule)).one().id
    assert client.post(f"/api/schedules/{schedule_id}/cancel").json()["status"] == "CANCELLED"
    [cancelled] = items(client)
    assert cancelled.status == "CANCELLED"
    move_due(client, cancelled.id)
    assert client.post("/api/reminders/scan").json()["scheduled"] == 0
    assert client.get("/api/invoices").json()[0]["status"] == "SENT"


def test_instalment_plan_waits_for_approval_and_sums_exactly(client):
    response = client.post(
        "/invoices", data=dict(FORM, amount="1000.00", instalments="3"), follow_redirects=False
    )
    assert response.status_code == 303
    assert client.get("/api/invoices").json() == [], "a plan sends nothing until approved"
    plan = items(client)
    assert [item.amount_cents for item in plan] == [33_334, 33_333, 33_333]
    assert [item.status for item in plan] == ["PENDING", "UPCOMING", "UPCOMING"]
    assert plan[0].description == "Monthly retainer (instalment 1 of 3)"

    client.post(f"/api/scheduled/{plan[0].id}/approve")
    for item in plan[1:]:
        move_due(client, item.id)
    client.post("/api/reminders/scan")
    for item in plan[1:]:
        client.post(f"/api/scheduled/{item.id}/approve")

    with Session(client.app.state.engine) as session:
        invoices = session.exec(select(Invoice)).all()
        assert sum(invoice.amount_cents for invoice in invoices) == 100_000
        assert all(invoice.paypal_invoice_id for invoice in invoices)
        assert session.exec(select(Schedule)).one().status == "DONE"


def test_approved_instalment_gets_reminders_like_any_invoice(client):
    client.post("/invoices", data=dict(FORM, instalments="2", terms_days="2"), follow_redirects=False)
    first = items(client)[0]
    client.post(f"/api/scheduled/{first.id}/approve")
    with Session(client.app.state.engine) as session:
        invoice = session.exec(select(Invoice)).one()
        invoice.risk_level = "HIGH"
        session.add(invoice)
        session.commit()
    assert client.post("/api/reminders/scan").json()["created"] == 1


def test_scheduled_invoice_cannot_be_approved_twice(client):
    client.post("/invoices", data=dict(FORM, instalments="2"), follow_redirects=False)
    first = items(client)[0]
    assert client.post(f"/api/scheduled/{first.id}/approve").status_code == 200
    assert client.post(f"/api/scheduled/{first.id}/approve").status_code == 409
    assert len(client.app.state.gateway.invoices) == 1


@pytest.mark.parametrize(
    "extra, message",
    [
        ({"instalments": "7"}, "between 2 and 6 instalments"),
        ({"instalments": "3", "repeat": "MONTH"}, "not both"),
        ({"repeat": "DAY"}, "weekly or monthly"),
        ({"repeat": "MONTH", "repeat_cycles": "0"}, "from 1 to 60"),
    ],
)
def test_bad_schedule_options_are_rejected(client, extra, message):
    response = client.post("/invoices", data=dict(FORM, **extra))
    assert response.status_code == 422
    assert message in response.text
    assert client.get("/api/invoices").json() == []


def test_risk_preview_flags_a_late_payer_without_saving(client):
    now = utcnow()
    with Session(client.app.state.engine) as session:
        from app.models import Client

        acme = Client(name="Acme Studio", email="billing@acme.test", user_id=client.user_id)
        session.add(acme)
        session.commit()
        for index in range(4):
            due = now - timedelta(days=200 - index * 40)
            session.add(
                Invoice(user_id=client.user_id, client_id=acme.id, invoice_number=f"OLD-{index}", amount_cents=100_000,
                        status="PAID", issued_at=due - timedelta(days=30), due_at=due,
                        paid_at=due + timedelta(days=25))
            )
        session.commit()
    preview = client.post("/api/risk/preview", data=FORM).json()
    assert preview["level"] == "HIGH"
    new_client = client.post("/api/risk/preview", data=dict(FORM, client_email="new@example.test")).json()
    assert new_client["level"] != "HIGH"
    assert len(client.get("/api/invoices").json()) == 4


def test_dashboard_lists_pending_and_active_schedules(client):
    client.post("/invoices", data=dict(FORM, instalments="2"), follow_redirects=False)
    page = client.get("/").text
    assert "Monthly retainer (instalment 1 of 2)" in page
    assert "in 2 parts, 0 sent" in page
    assert "Approve and send" in page
