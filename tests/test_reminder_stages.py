"""One sent reminder per stage: before due, then overdue (issue #42)."""

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import inspect, text
from sqlmodel import Session, select

from app.mailer import MailError, MockMailer
from app.models import Invoice, Reminder, init_db, make_engine, utcnow
from app.reminders import draft_reminders, is_overdue, reject_reminder, reminder_stage, send_reminder
from app.services import dashboard_totals, invoice_rows
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


def _partly_send_then_reject(client, session, reminder):
    """Approve for both channels; PayPal delivers, email fails; then reject (issue #76)."""
    mailer = MockMailer()
    mailer.fail_with = "smtp down"
    with pytest.raises(MailError):
        send_reminder(session, reminder, client.app.state.gateway, mailer, "both")
    session.refresh(reminder)
    assert reminder.sent_channels == "paypal"
    reject_reminder(session, reminder)


def test_a_partly_sent_then_rejected_reminder_blocks_its_stage(client):
    seed_invoice(client, due_days=-3)
    gateway = client.app.state.gateway
    with Session(client.app.state.engine) as session:
        [draft] = _scan(client, session)
        _partly_send_then_reject(client, session, draft)
        assert _scan(client, session) == []
        assert _scan(client, session, utcnow() + timedelta(days=30)) == []
    assert len(gateway.sent_reminders) == 1


def test_a_partly_sent_before_due_reminder_still_lets_the_overdue_stage_draft(client):
    seed_invoice(client, due_days=2, risk="MEDIUM")
    with Session(client.app.state.engine) as session:
        [friendly] = _scan(client, session)
        _partly_send_then_reject(client, session, friendly)
        assert _scan(client, session) == []
        [firm] = _scan(client, session, utcnow() + timedelta(days=30))
    assert firm.stage == "OVERDUE"


@pytest.mark.parametrize("channels", ["paypal", "email", "email,paypal"])
def test_a_rejected_reminder_any_channel_delivered_counts_as_sent(client, channels):
    invoice_id = seed_invoice(client, due_days=-3)
    with Session(client.app.state.engine) as session:
        session.add(
            Reminder(
                user_id=client.user_id,
                invoice_id=invoice_id,
                draft_text="partly sent",
                stage="OVERDUE",
                status="REJECTED",
                sent_channels=channels,
                sent_at=utcnow(),
            )
        )
        session.commit()
        assert _scan(client, session) == []


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


# One overdue test for eligibility, stage, wording and the dashboard flag:
# the UTC due day has ended (issues #80, #119). Fixed times keep the day boundary
# out of the clock's hands.

DUE_AT = datetime(2026, 11, 11, 9, 0, tzinfo=timezone.utc)
LATER_ON_THE_DUE_DAY = datetime(2026, 11, 11, 23, 59, tzinfo=timezone.utc)
AFTER_THE_DUE_DAY = datetime(2026, 11, 12, 0, 1, tzinfo=timezone.utc)


def _seed_due_at(client, due_at, risk, terms_days=14):
    invoice_id = seed_invoice(client, risk=risk)
    with Session(client.app.state.engine) as session:
        invoice = session.get(Invoice, invoice_id)
        invoice.due_at = due_at
        invoice.terms_days = terms_days
        session.add(invoice)
        session.commit()
    return invoice_id


def _overdue_flag(client, now):
    with Session(client.app.state.engine) as session:
        rows = invoice_rows(session, client.user_id, now=now)
    return rows[0]["overdue"], dashboard_totals(rows)["overdue_count"]


def test_an_invoice_due_earlier_today_is_not_overdue(client):
    invoice_id = _seed_due_at(client, DUE_AT, "LOW")
    with Session(client.app.state.engine) as session:
        invoice = session.get(Invoice, invoice_id)
        assert not is_overdue(invoice, LATER_ON_THE_DUE_DAY)
        assert reminder_stage(invoice, LATER_ON_THE_DUE_DAY) == "BEFORE_DUE"
        assert _scan(client, session, LATER_ON_THE_DUE_DAY) == []
    assert _overdue_flag(client, LATER_ON_THE_DUE_DAY) == (False, 0)


def test_an_invoice_is_overdue_once_its_due_day_ends(client):
    _seed_due_at(client, DUE_AT, "LOW")
    with Session(client.app.state.engine) as session:
        [draft] = _scan(client, session, AFTER_THE_DUE_DAY)
        assert draft.stage == "OVERDUE"
        assert "was due on 11 Nov 2026" in draft.draft_text
        assert "is due on" not in draft.draft_text
        _send(client, session, draft)
        assert _scan(client, session, AFTER_THE_DUE_DAY + timedelta(days=1)) == []
    assert _overdue_flag(client, AFTER_THE_DUE_DAY) == (True, 1)


def test_a_medium_risk_invoice_on_its_due_day_gets_before_due_wording(client):
    _seed_due_at(client, DUE_AT, "MEDIUM")
    with Session(client.app.state.engine) as session:
        [draft] = _scan(client, session, LATER_ON_THE_DUE_DAY)
    assert draft.stage == "BEFORE_DUE"
    assert "is due on 11 Nov 2026" in draft.draft_text
    assert "was due on" not in draft.draft_text


def test_an_invoice_on_terms_of_0_days_is_not_overdue_on_its_first_day(client):
    _seed_due_at(client, DUE_AT, "LOW", terms_days=0)
    with Session(client.app.state.engine) as session:
        assert _scan(client, session, DUE_AT + timedelta(minutes=1)) == []
        [draft] = _scan(client, session, AFTER_THE_DUE_DAY)
    assert draft.stage == "OVERDUE"
    assert _overdue_flag(client, DUE_AT + timedelta(minutes=1)) == (False, 0)


def test_an_invoice_not_yet_due_gets_before_due_wording(client):
    seed_invoice(client, due_days=1 / 12, risk="MEDIUM")
    with Session(client.app.state.engine) as session:
        [draft] = _scan(client, session)
    assert draft.stage == "BEFORE_DUE"
    assert "is due on" in draft.draft_text


def test_a_reminder_without_a_stage_drafted_later_on_the_due_day_counts_as_before_due(client):
    invoice_id = _seed_due_at(client, DUE_AT, "LOW")
    _legacy_sent_reminder(client, invoice_id, DUE_AT + timedelta(hours=2))
    with Session(client.app.state.engine) as session:
        [firm] = _scan(client, session, AFTER_THE_DUE_DAY)
    assert firm.stage == "OVERDUE"


def test_a_reminder_without_a_stage_drafted_after_the_due_day_counts_as_overdue(client):
    invoice_id = _seed_due_at(client, DUE_AT, "LOW")
    _legacy_sent_reminder(client, invoice_id, AFTER_THE_DUE_DAY)
    with Session(client.app.state.engine) as session:
        assert _scan(client, session, AFTER_THE_DUE_DAY + timedelta(hours=1)) == []
