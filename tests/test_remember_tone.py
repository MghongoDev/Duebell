"""The next draft for a client starts in the tone the user last sent them (issue #153)."""

from datetime import timedelta

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlmodel import Session

from app.models import Client, Invoice, Reminder, init_db, make_engine, utcnow
from app.ownership import EVERY_USER
from app.reminders import draft_reminders
from tests.conftest import sign_in
from tests.test_reminders import seed_invoice

FIRM_ASK = "Please confirm the payment date this week."
FRIENDLY_ASK = "Could you confirm the payment date?"
FIRM_REASON = "You sent Firm to this client last time."
FRIENDLY_REASON = "You sent Friendly to this client last time."


def scan(client) -> list[dict]:
    """Scan as the signed-in user and return their pending drafts, newest first."""
    client.post("/api/reminders/scan")
    return [row for row in client.get("/api/reminders").json() if row["status"] == "PENDING"]


def draft_for(client, invoice_id) -> dict:
    return next(row for row in scan(client) if row["invoice_id"] == invoice_id)


def sent_in(client, tone, **seed) -> int:
    """Draft a reminder, switch it to `tone`, approve it, and return its id."""
    invoice_id = seed_invoice(client, **seed)
    reminder_id = draft_for(client, invoice_id)["id"]
    assert client.post(f"/api/reminders/{reminder_id}/tone", data={"tone": tone}).status_code == 200
    assert client.post(f"/api/reminders/{reminder_id}/approve").status_code == 200
    return reminder_id


def test_the_next_draft_starts_in_the_tone_last_sent(client):
    sent_in(client, "firm", risk="LOW")
    invoice_id = seed_invoice(client, risk="LOW")
    draft = draft_for(client, invoice_id)
    assert draft["tone"] == "firm"
    assert FIRM_ASK in draft["draft_text"]
    assert draft["tone_reason"] == FIRM_REASON
    assert draft["chosen_tone"] is None


def test_a_remembered_friendly_tone_beats_the_risk_rule(client):
    sent_in(client, "friendly", risk="HIGH")
    invoice_id = seed_invoice(client, risk="HIGH")
    draft = draft_for(client, invoice_id)
    assert draft["tone"] == "friendly"
    assert FRIENDLY_ASK in draft["draft_text"]
    assert draft["tone_reason"] == FRIENDLY_REASON


def test_without_a_remembered_choice_the_rule_applies(client):
    invoice_id = seed_invoice(client, risk="HIGH")
    draft = draft_for(client, invoice_id)
    assert draft["tone"] == "firm"
    assert draft["tone_reason"] is None


def test_a_sent_reminder_the_user_never_switched_is_not_remembered(client):
    first = seed_invoice(client, risk="HIGH")
    reminder_id = draft_for(client, first)["id"]
    client.post(f"/api/reminders/{reminder_id}/approve")
    invoice_id = seed_invoice(client, risk="LOW")
    draft = draft_for(client, invoice_id)
    assert draft["tone"] == "friendly"
    assert draft["tone_reason"] is None


def test_a_pending_or_rejected_choice_is_not_remembered(client):
    rejected = draft_for(client, seed_invoice(client, risk="LOW"))["id"]
    client.post(f"/api/reminders/{rejected}/tone", data={"tone": "firm"})
    client.post(f"/api/reminders/{rejected}/reject")
    pending = draft_for(client, seed_invoice(client, risk="LOW"))["id"]
    client.post(f"/api/reminders/{pending}/tone", data={"tone": "firm"})

    draft = draft_for(client, seed_invoice(client, risk="LOW"))
    assert draft["tone"] == "friendly"
    assert draft["tone_reason"] is None


def test_the_most_recently_sent_choice_wins_over_the_newest_row(client):
    firm = sent_in(client, "firm", risk="LOW")
    friendly = sent_in(client, "friendly", risk="LOW")
    assert friendly > firm
    # The friendly reminder has the higher id but went out earlier.
    with Session(client.app.state.engine) as session:
        session.get(Reminder, firm).sent_at = utcnow() - timedelta(hours=1)
        session.get(Reminder, friendly).sent_at = utcnow() - timedelta(days=2)
        session.commit()
    draft = draft_for(client, seed_invoice(client, risk="LOW"))
    assert draft["tone"] == "firm"
    assert draft["tone_reason"] == FIRM_REASON


def test_another_clients_choice_is_not_remembered(client):
    sent_in(client, "firm", risk="LOW")
    with Session(client.app.state.engine) as session:
        other = seed_invoice(client, risk="LOW")
        invoice = session.get(Invoice, other)
        stranger = Client(name="Other Co", email="ap@other.test", user_id=client.user_id)
        session.add(stranger)
        session.commit()
        invoice.client_id = stranger.id
        session.add(invoice)
        session.commit()
    draft = draft_for(client, other)
    assert draft["tone"] == "friendly"
    assert draft["tone_reason"] is None


def test_one_users_choice_never_reaches_another_users_draft(client):
    # Both users bill a client with the same name and email (seed_invoice).
    sent_in(client, "firm", risk="LOW")
    other = sign_in(TestClient(client.app), email="other@duebell.test")
    other_invoice = seed_invoice(other, risk="LOW")
    own_invoice = seed_invoice(client, risk="LOW")

    # The scheduler drafts for every user at once.
    with Session(client.app.state.engine) as session:
        drafts = {r.invoice_id: r for r in draft_reminders(session, client.app.state.settings, scope=EVERY_USER)}
        assert drafts[other_invoice].tone == "friendly"
        assert drafts[other_invoice].tone_reason is None
        assert drafts[own_invoice].tone == "firm"
        assert drafts[own_invoice].tone_reason == FIRM_REASON


def test_scan_now_keeps_one_users_choice_from_another(client):
    sent_in(client, "firm", risk="LOW")
    other = sign_in(TestClient(client.app), email="other@duebell.test")
    draft = draft_for(other, seed_invoice(other, risk="LOW"))
    assert draft["tone"] == "friendly"
    assert draft["tone_reason"] is None


def test_switching_or_editing_the_draft_drops_the_reason(client):
    sent_in(client, "firm", risk="LOW")
    switched = draft_for(client, seed_invoice(client, risk="LOW"))["id"]
    client.post(f"/api/reminders/{switched}/tone", data={"tone": "friendly"})
    client.post(f"/api/reminders/{switched}/reject")
    edited = draft_for(client, seed_invoice(client, risk="LOW"))
    assert edited["tone_reason"] == FIRM_REASON
    client.post(f"/api/reminders/{edited['id']}/edit", data={"draft_text": "My own words."})
    with Session(client.app.state.engine) as session:
        assert session.get(Reminder, switched).tone_reason is None
        assert session.get(Reminder, edited["id"]).tone_reason is None


def test_the_dashboard_shows_the_reason(client):
    sent_in(client, "firm", risk="LOW")
    draft = draft_for(client, seed_invoice(client, risk="LOW"))
    page = client.get("/").text
    assert f'data-tone-reason-for="{draft["id"]}"' in page
    assert FIRM_REASON in page


def test_the_dashboard_escapes_the_reason(client):
    draft = draft_for(client, seed_invoice(client, risk="LOW"))
    with Session(client.app.state.engine) as session:
        session.get(Reminder, draft["id"]).tone_reason = "<script>alert(1)</script>"
        session.commit()
    page = client.get("/").text
    assert "<script>alert(1)</script>" not in page
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in page


def test_an_older_database_gains_the_tone_reason_column(tmp_path):
    url = f"sqlite:///{tmp_path / 'old.db'}"
    engine = make_engine(url)
    init_db(engine)
    with engine.begin() as connection:
        connection.execute(text('ALTER TABLE "reminder" DROP COLUMN "tone_reason"'))
    init_db(create_engine(url))
    with engine.connect() as connection:
        columns = {row[1] for row in connection.execute(text('PRAGMA table_info("reminder")'))}
    assert "tone_reason" in columns
