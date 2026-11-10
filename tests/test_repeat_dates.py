"""Monthly repeats keep their day of the month (issue #43)."""

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine, inspect, text
from sqlmodel import Session, select

from app.models import Client, Invoice, Schedule, ScheduledInvoice, init_db, make_engine
from app.ownership import EVERY_USER
from app.schedules import ScheduleBasis, create_plan, create_recurring, draft_scheduled
from tests.test_schedules import FORM


def day(year, month, day_of_month):
    return datetime(year, month, day_of_month, tzinfo=timezone.utc)


def _basis(session, user_id):
    client = Client(name="Acme Studio", email="billing@acme.test", user_id=user_id)
    session.add(client)
    session.commit()
    return ScheduleBasis(
        user_id=user_id,
        client_id=client.id,
        description="Monthly retainer",
        amount_cents=100_000,
        currency="USD",
        terms_days=14,
    )


def _upcoming(session, schedule):
    query = select(ScheduledInvoice).where(
        ScheduledInvoice.schedule_id == schedule.id, ScheduledInvoice.status == "UPCOMING"
    )
    return session.exec(query).one()


def _run(session, schedule, occurrences):
    """Scan on each upcoming date in turn and return every issue date, in order."""
    for _ in range(occurrences - 1):
        draft_scheduled(session, now=_upcoming(session, schedule).issue_on, scope=EVERY_USER)
    query = select(ScheduledInvoice).where(ScheduledInvoice.schedule_id == schedule.id)
    return [item.issue_on for item in session.exec(query.order_by(ScheduledInvoice.sequence)).all()]


def _repeat(client, start, unit="MONTH"):
    with Session(client.app.state.engine) as session:
        schedule = create_recurring(session, _basis(session, client.user_id), interval_unit=unit, start=start)
        return _run(session, schedule, 12)


def test_a_repeat_from_the_31st_runs_on_each_month_end(client):
    assert _repeat(client, day(2026, 1, 31)) == [
        day(2026, 2, 28),
        day(2026, 3, 31),
        day(2026, 4, 30),
        day(2026, 5, 31),
        day(2026, 6, 30),
        day(2026, 7, 31),
        day(2026, 8, 31),
        day(2026, 9, 30),
        day(2026, 10, 31),
        day(2026, 11, 30),
        day(2026, 12, 31),
        day(2027, 1, 31),
    ]


def test_a_repeat_from_the_29th_returns_to_the_29th_after_february(client):
    assert _repeat(client, day(2026, 1, 29)) == [day(2026, 2, 28)] + [
        day(2026 + (month > 12), (month - 1) % 12 + 1, 29) for month in range(3, 14)
    ]


@pytest.mark.parametrize("start_day", [29, 31])
def test_a_leap_year_february_takes_the_29th(client, start_day):
    dates = _repeat(client, day(2028, 1, start_day))
    assert dates[:3] == [day(2028, 2, 29), day(2028, 3, start_day), day(2028, 4, min(start_day, 30))]


def test_weekly_repeats_keep_their_dates(client):
    start = day(2026, 1, 28)
    assert _repeat(client, start, unit="WEEK") == [start + timedelta(weeks=n) for n in range(1, 13)]


def test_instalment_plans_keep_their_dates(client):
    with Session(client.app.state.engine) as session:
        basis = _basis(session, client.user_id)
        create_plan(session, basis, instalments=4, start=day(2026, 1, 31))
        dates = [item.issue_on for item in session.exec(select(ScheduledInvoice).order_by(ScheduledInvoice.sequence))]
    assert dates == [day(2026, 1, 31), day(2026, 2, 28), day(2026, 3, 31), day(2026, 4, 30)]


def test_the_form_counts_a_repeat_from_the_invoice_it_sent(client):
    client.post("/invoices", data=dict(FORM, repeat="MONTH"), follow_redirects=False)
    with Session(client.app.state.engine) as session:
        invoice = session.exec(select(Invoice)).one()
        schedule = session.exec(select(Schedule)).one()
        assert schedule.starts_on == invoice.issued_at


def test_a_schedule_from_an_older_build_counts_from_its_first_item(client):
    """Without starts_on, the repeat counts from its first scheduled invoice."""
    with Session(client.app.state.engine) as session:
        schedule = create_recurring(
            session, _basis(session, client.user_id), interval_unit="MONTH", start=day(2026, 1, 31)
        )
        schedule.starts_on = None
        session.add(schedule)
        first = _upcoming(session, schedule)
        first.issue_on = day(2026, 1, 31)
        session.add(first)
        session.commit()
        assert _run(session, schedule, 4) == [day(2026, 1, 31), day(2026, 2, 28), day(2026, 3, 31), day(2026, 4, 30)]


def test_an_old_database_gains_the_start_column(tmp_path):
    url = f"sqlite:///{tmp_path / 'old.db'}"
    old = create_engine(url)
    with old.begin() as connection:
        connection.execute(
            text(
                "CREATE TABLE schedule (id INTEGER PRIMARY KEY, user_id INTEGER, kind VARCHAR, client_id INTEGER, "
                "description VARCHAR, amount_cents INTEGER, currency VARCHAR, terms_days INTEGER, "
                "interval_unit VARCHAR, interval_count INTEGER, cycles_left INTEGER, status VARCHAR, "
                "created_at DATETIME)"
            )
        )
        connection.execute(text("INSERT INTO schedule (client_id, amount_cents) VALUES (1, 100)"))
    old.dispose()
    engine = make_engine(url)
    init_db(engine)
    assert "starts_on" in {column["name"] for column in inspect(engine).get_columns("schedule")}
    with Session(engine) as session:
        assert session.exec(select(Schedule)).one().starts_on is None
