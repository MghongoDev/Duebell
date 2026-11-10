"""The scheduler and a manual scan act once on each item (issue #40)."""

import re
import threading

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event, inspect, text
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, select

from app.config import Settings
from app.models import Reminder, Schedule, ScheduledInvoice, init_db, make_engine
from app.ownership import EVERY_USER
from app.reminders import draft_reminders
from app.schedules import draft_scheduled
from app.web import create_app
from tests.conftest import sign_in
from tests.test_reminders import seed_invoice
from tests.test_schedules import FORM, items, move_due


@pytest.fixture
def file_client(tmp_path):
    """A signed-in client on a file database, so two threads use two connections."""
    settings = Settings(
        database_url=f"sqlite:///{tmp_path / 'scan.db'}",
        amount_cap_cents=1_000_000,
        allowed_currencies=("USD",),
        terms_min_days=0,
        terms_max_days=120,
    )
    app = create_app(settings=settings, engine=make_engine(settings.database_url))
    return sign_in(TestClient(app))


def _scheduler_paused_at(client, table, scan):
    """Run `scan` the way the scheduler does, in its own thread.

    It pauses at its first read of `table`: after it has read the work list,
    before it writes anything. Returns (thread, paused, let_go).
    """
    engine = client.app.state.engine
    paused, let_go = threading.Event(), threading.Event()
    pattern = re.compile(rf"FROM {table}\b")
    worker_ids = []

    def hold(conn, cursor, statement, parameters, context, executemany):
        if threading.get_ident() in worker_ids and not paused.is_set() and pattern.search(statement):
            paused.set()
            let_go.wait(timeout=5)

    def run():
        worker_ids.append(threading.get_ident())
        with Session(engine) as session:
            scan(session)

    event.listen(engine, "before_cursor_execute", hold)
    worker = threading.Thread(target=run)
    worker.start()
    assert paused.wait(timeout=5), "the scheduler never reached its pause"
    return worker, let_go


def test_two_scans_at_once_queue_one_next_occurrence(file_client):
    client = file_client
    client.post("/invoices", data=dict(FORM, repeat="MONTH"), follow_redirects=False)
    [upcoming] = items(client)
    move_due(client, upcoming.id)

    worker, let_go = _scheduler_paused_at(
        client, "schedule", lambda session: draft_scheduled(session, scope=EVERY_USER)
    )
    manual = client.post("/api/reminders/scan")
    let_go.set()
    worker.join(timeout=5)

    assert manual.json()["scheduled"] == 1
    assert [item.status for item in items(client)] == ["PENDING", "UPCOMING"]
    assert [item.sequence for item in items(client)] == [1, 2]


def test_two_scans_at_once_draft_one_reminder(file_client):
    client = file_client
    invoice_id = seed_invoice(client)
    settings = client.app.state.settings

    worker, let_go = _scheduler_paused_at(
        client, "client", lambda session: draft_reminders(session, settings, scope=EVERY_USER)
    )
    manual = client.post("/api/reminders/scan")
    let_go.set()
    worker.join(timeout=5)

    assert manual.json()["created"] == 1
    with Session(client.app.state.engine) as session:
        drafts = session.exec(select(Reminder).where(Reminder.invoice_id == invoice_id)).all()
    assert [draft.status for draft in drafts] == ["PENDING"]


def test_the_database_refuses_a_second_item_with_the_same_sequence(client):
    client.post("/invoices", data=dict(FORM, repeat="MONTH"), follow_redirects=False)
    [upcoming] = items(client)
    with Session(client.app.state.engine) as session:
        session.add(
            ScheduledInvoice(
                user_id=upcoming.user_id,
                schedule_id=upcoming.schedule_id,
                sequence=upcoming.sequence,
                amount_cents=upcoming.amount_cents,
                issue_on=upcoming.issue_on,
            )
        )
        with pytest.raises(IntegrityError):
            session.commit()


def test_the_database_refuses_a_second_pending_reminder(client):
    invoice_id = seed_invoice(client)
    with Session(client.app.state.engine) as session:
        session.add(Reminder(user_id=client.user_id, invoice_id=invoice_id, draft_text="one", status="REJECTED"))
        session.add(Reminder(user_id=client.user_id, invoice_id=invoice_id, draft_text="two"))
        session.commit()
        session.add(Reminder(user_id=client.user_id, invoice_id=invoice_id, draft_text="three"))
        with pytest.raises(IntegrityError):
            session.commit()


def test_a_scan_skips_an_invoice_that_already_has_a_pending_draft(client, monkeypatch):
    """The scan's own check can be stale; the index catches it and the scan goes on."""
    first = seed_invoice(client)
    second = seed_invoice(client)
    with Session(client.app.state.engine) as session:
        session.add(Reminder(user_id=client.user_id, invoice_id=first, draft_text="earlier"))
        session.commit()
        monkeypatch.setattr("app.reminders._blocked_invoice_ids", lambda session: set())
        created = draft_reminders(session, client.app.state.settings, scope=client.user_id)
    assert [reminder.invoice_id for reminder in created] == [second]


def _old_database(url, *rows):
    """The two tables as a build before issue #40 made them, without the new indexes."""
    old = create_engine(url)
    with old.begin() as connection:
        connection.execute(
            text(
                "CREATE TABLE scheduledinvoice (id INTEGER PRIMARY KEY, user_id INTEGER, schedule_id INTEGER, "
                "sequence INTEGER, amount_cents INTEGER, description VARCHAR, issue_on DATETIME, "
                "status VARCHAR, invoice_id INTEGER, created_at DATETIME)"
            )
        )
        connection.execute(
            text(
                "CREATE TABLE reminder (id INTEGER PRIMARY KEY, user_id INTEGER, invoice_id INTEGER, "
                "draft_text VARCHAR, tone VARCHAR, status VARCHAR, created_at DATETIME, sent_at DATETIME, "
                "sent_channels VARCHAR)"
            )
        )
        for row in rows:
            connection.execute(text(row))
    old.dispose()


def _unique_indexes(engine, table):
    return {index["name"] for index in inspect(engine).get_indexes(table) if index["unique"]}


def test_an_old_database_gains_both_indexes_on_startup(tmp_path):
    url = f"sqlite:///{tmp_path / 'old.db'}"
    _old_database(url)
    engine = make_engine(url)
    init_db(engine)
    assert "ux_scheduledinvoice_schedule_sequence" in _unique_indexes(engine, "scheduledinvoice")
    assert "ux_reminder_pending_invoice" in _unique_indexes(engine, "reminder")


def test_an_old_database_with_duplicates_still_starts(tmp_path, caplog):
    url = f"sqlite:///{tmp_path / 'old.db'}"
    _old_database(
        url,
        "INSERT INTO scheduledinvoice (schedule_id, sequence, amount_cents, status) VALUES (1, 2, 100, 'UPCOMING')",
        "INSERT INTO scheduledinvoice (schedule_id, sequence, amount_cents, status) VALUES (1, 2, 100, 'UPCOMING')",
        "INSERT INTO reminder (invoice_id, draft_text, status) VALUES (1, 'a', 'PENDING')",
        "INSERT INTO reminder (invoice_id, draft_text, status) VALUES (1, 'b', 'PENDING')",
    )
    engine = make_engine(url)
    init_db(engine)

    # The rows are left alone and the indexes wait for the next startup.
    assert "ux_scheduledinvoice_schedule_sequence" not in _unique_indexes(engine, "scheduledinvoice")
    assert "ux_reminder_pending_invoice" not in _unique_indexes(engine, "reminder")
    assert "ux_reminder_pending_invoice" in caplog.text

    with engine.begin() as connection:
        connection.execute(text("UPDATE reminder SET status = 'REJECTED' WHERE draft_text = 'b'"))
    init_db(engine)
    assert "ux_reminder_pending_invoice" in _unique_indexes(engine, "reminder")


def test_a_due_item_still_becomes_pending_when_its_next_occurrence_exists(client):
    """The index refuses a second next occurrence; the item keeps its claim anyway."""
    client.post("/invoices", data=dict(FORM, repeat="MONTH", repeat_cycles="3"), follow_redirects=False)
    [upcoming] = items(client)
    with Session(client.app.state.engine) as session:
        session.add(
            ScheduledInvoice(
                user_id=upcoming.user_id,
                schedule_id=upcoming.schedule_id,
                sequence=upcoming.sequence + 1,
                amount_cents=upcoming.amount_cents,
                issue_on=upcoming.issue_on,
            )
        )
        session.commit()
        cycles_left = session.get(Schedule, upcoming.schedule_id).cycles_left
    move_due(client, upcoming.id)

    assert client.post("/api/reminders/scan").json()["scheduled"] == 1
    assert [(item.sequence, item.status) for item in items(client)] == [(1, "PENDING"), (2, "UPCOMING")]
    with Session(client.app.state.engine) as session:
        assert session.get(Schedule, upcoming.schedule_id).cycles_left == cycles_left
