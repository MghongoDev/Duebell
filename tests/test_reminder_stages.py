"""One sent reminder per stage: before due, then overdue (issue #42)."""

from datetime import timedelta

from sqlalchemy import inspect, text
from sqlmodel import Session, select

from app.models import Invoice, Reminder, init_db, make_engine, utcnow
from app.reminders import draft_reminders, reject_reminder, send_reminder
from tests.test_reminders import seed_invoice
from tests.test_scan_once import _old_database


def _scan(client, session, now=None):
    return draft_reminders(session, client.app.state.settings, now, scope=client.user_id)


def _send(client, session, reminder):
    return send_reminder(session, reminder, client.app.state.gateway).reminder


def test_a_draft_records_its_stage(client):
    seed_invoice(client, due_days=2, risk="MEDIUM")
    seed_invoice(client, due_days=-3, risk="LOW")
    with Session(client.app.state.engine) as session:
        stages = sorted(reminder.stage for reminder in _scan(client, session))
    assert stages == ["BEFORE_DUE", "OVERDUE"]


def test_a_scan_drafts_an_overdue_reminder_after_a_before_due_one(client):
    invoice_id = seed_invoice(client, due_days=2, risk="MEDIUM")
    with Session(client.app.state.engine) as session:
        [friendly] = _scan(client, session)
        assert friendly.stage == "BEFORE_DUE"
        _send(client, session, friendly)

        later = utcnow() + timedelta(days=30)
        [firm] = _scan(client, session, later)
    assert firm.invoice_id == invoice_id
    assert firm.stage == "OVERDUE"
    assert firm.tone == "firm"


def test_a_sent_before_due_reminder_blocks_another_before_due(client):
    seed_invoice(client, due_days=3, risk="HIGH")
    with Session(client.app.state.engine) as session:
        [friendly] = _scan(client, session)
        _send(client, session, friendly)
        assert _scan(client, session, utcnow() + timedelta(days=1)) == []


def test_a_sent_overdue_reminder_blocks_another_overdue(client):
    seed_invoice(client, due_days=-3)
    with Session(client.app.state.engine) as session:
        [firm] = _scan(client, session)
        _send(client, session, firm)
        assert _scan(client, session) == []
        assert _scan(client, session, utcnow() + timedelta(days=30)) == []


def test_a_pending_draft_blocks_a_draft_in_the_next_stage(client):
    seed_invoice(client, due_days=2, risk="MEDIUM")
    with Session(client.app.state.engine) as session:
        assert len(_scan(client, session)) == 1
        assert _scan(client, session, utcnow() + timedelta(days=30)) == []


def test_a_rejected_draft_blocks_nothing(client):
    seed_invoice(client, due_days=-3)
    with Session(client.app.state.engine) as session:
        [draft] = _scan(client, session)
        reject_reminder(session, draft)
        [again] = _scan(client, session)
    assert again.stage == "OVERDUE"


def _legacy_sent_reminder(client, invoice_id, created_at):
    """A sent reminder from a build before issue #42, so it has no stage."""
    with Session(client.app.state.engine) as session:
        session.add(
            Reminder(
                user_id=client.user_id,
                invoice_id=invoice_id,
                draft_text="old",
                status="SENT",
                created_at=created_at,
                sent_at=created_at,
            )
        )
        session.commit()


def test_a_reminder_without_a_stage_drafted_before_the_due_date_counts_as_before_due(client):
    invoice_id = seed_invoice(client, due_days=-3)
    with Session(client.app.state.engine) as session:
        due_at = session.get(Invoice, invoice_id).due_at
    _legacy_sent_reminder(client, invoice_id, due_at - timedelta(days=2))
    with Session(client.app.state.engine) as session:
        [firm] = _scan(client, session)
    assert firm.stage == "OVERDUE"


def test_a_reminder_without_a_stage_drafted_after_the_due_date_counts_as_overdue(client):
    invoice_id = seed_invoice(client, due_days=-3)
    with Session(client.app.state.engine) as session:
        due_at = session.get(Invoice, invoice_id).due_at
    _legacy_sent_reminder(client, invoice_id, due_at + timedelta(days=2))
    with Session(client.app.state.engine) as session:
        assert _scan(client, session) == []


def test_an_old_database_gains_an_empty_stage_column(tmp_path):
    url = f"sqlite:///{tmp_path / 'old.db'}"
    _old_database(url, "INSERT INTO reminder (invoice_id, draft_text, status) VALUES (1, 'a', 'SENT')")
    engine = make_engine(url)
    init_db(engine)
    assert "stage" in {column["name"] for column in inspect(engine).get_columns("reminder")}
    with engine.begin() as connection:
        assert connection.execute(text("SELECT stage FROM reminder")).scalar_one() is None
    with Session(engine) as session:
        assert session.exec(select(Reminder)).one().stage is None
