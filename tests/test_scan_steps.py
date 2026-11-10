"""Each step of the scheduler's scan runs on its own (issue #81)."""

import logging

import pytest
from sqlalchemy.exc import OperationalError
from sqlmodel import Session, select

from app.jobs import start_scheduler, stop_scheduler
from app.models import Reminder
from tests.test_reminders import seed_invoice
from tests.test_schedules import FORM, items, move_due


def _locked(session, *args, **kwargs):
    """Fail the way a locked database does, after starting a transaction."""
    session.exec(select(Reminder)).all()
    raise OperationalError("COMMIT", {}, Exception("database is locked"))


@pytest.fixture
def tick(client):
    """The scheduler's own scan job, called once by hand."""
    scheduler = start_scheduler(client.app)
    try:
        yield scheduler.get_job("reminder-scan").func
    finally:
        stop_scheduler(client.app)


def _reminders(client):
    with Session(client.app.state.engine) as session:
        return session.exec(select(Reminder)).all()


def _due_repeat(client):
    client.post("/invoices", data=dict(FORM, repeat="MONTH"), follow_redirects=False)
    [upcoming] = items(client)
    move_due(client, upcoming.id)


def test_a_failed_reminder_step_still_moves_scheduled_invoices(client, tick, monkeypatch, caplog):
    _due_repeat(client)
    monkeypatch.setattr("app.jobs.draft_reminders", _locked)

    with caplog.at_level(logging.ERROR, logger="duebell.jobs"):
        tick()

    assert [item.status for item in items(client)] == ["PENDING", "UPCOMING"]
    assert [record.getMessage() for record in caplog.records] == ["reminder drafting failed"]


def test_a_failed_scheduled_step_still_drafts_reminders(client, tick, monkeypatch, caplog):
    seed_invoice(client)
    monkeypatch.setattr("app.jobs.draft_scheduled", _locked)

    with caplog.at_level(logging.ERROR, logger="duebell.jobs"):
        tick()

    assert len(_reminders(client)) == 1
    assert [record.getMessage() for record in caplog.records] == ["scheduled invoice drafting failed"]
