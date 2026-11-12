"""The user switches a pending draft between friendly and firm (issue #152)."""

import json
import shutil
import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlmodel import Session

from app.models import Invoice, Reminder, init_db, make_engine
from app.reminders import draft_reminders, edit_reminder, switch_tone
from tests.conftest import sign_in
from tests.test_reminders import seed_invoice

FIRM_ASK = "Please confirm the payment date this week."
FRIENDLY_ASK = "Could you confirm the payment date?"
SCRIPT = Path(__file__).resolve().parent.parent / "app" / "static" / "dashboard.js"


def drafted(client, **seed):
    """Seed an invoice, scan, and return the pending draft's id."""
    seed_invoice(client, **seed)
    response = client.post("/api/reminders/scan")
    assert response.json()["created"] == 1
    return client.get("/api/reminders").json()[0]["id"]


def stored(client, reminder_id) -> Reminder:
    with Session(client.app.state.engine) as session:
        return session.get(Reminder, reminder_id)


def test_switching_rebuilds_the_text_in_the_chosen_tone(client):
    seed_invoice(client, due_days=-3, risk="LOW")
    with Session(client.app.state.engine) as session:
        reminder = draft_reminders(session, client.app.state.settings, scope=client.user_id)[0]
        assert reminder.tone == "friendly"
        invoice = session.get(Invoice, reminder.invoice_id)
        switch_tone(session, reminder, "firm", client.app.state.settings)
        assert reminder.tone == "firm"
        assert reminder.chosen_tone == "firm"
        assert reminder.status == "PENDING"
        assert FIRM_ASK in reminder.draft_text
        assert FRIENDLY_ASK not in reminder.draft_text
        assert invoice.invoice_number in reminder.draft_text
        assert "1,200.00 USD" in reminder.draft_text
        assert f"{invoice.due_at:%d %b %Y}" in reminder.draft_text
    assert client.app.state.gateway.sent_reminders == []


def test_switching_a_firm_draft_to_friendly(client):
    reminder_id = drafted(client, risk="HIGH")
    assert stored(client, reminder_id).tone == "firm"
    response = client.post(f"/api/reminders/{reminder_id}/tone", data={"tone": "friendly"})
    assert response.status_code == 200, response.text
    assert response.json()["tone"] == "friendly"
    assert FRIENDLY_ASK in response.json()["draft_text"]
    reminder = stored(client, reminder_id)
    assert reminder.tone == "friendly"
    assert reminder.chosen_tone == "friendly"
    assert FRIENDLY_ASK in reminder.draft_text


def test_a_scanned_draft_has_no_chosen_tone(client):
    reminder_id = drafted(client)
    assert stored(client, reminder_id).chosen_tone is None
    assert client.get("/api/reminders").json()[0]["chosen_tone"] is None


def test_switching_keeps_the_stage_wording(client):
    reminder_id = drafted(client, due_days=3, risk="HIGH")
    before = stored(client, reminder_id)
    assert before.stage == "BEFORE_DUE"
    response = client.post(f"/api/reminders/{reminder_id}/tone", data={"tone": "friendly"})
    assert response.status_code == 200
    after = stored(client, reminder_id)
    assert FRIENDLY_ASK in after.draft_text
    assert after.stage == "BEFORE_DUE"
    assert "is due on" in after.draft_text
    assert "was due on" not in after.draft_text


def test_an_unknown_tone_is_refused(client):
    reminder_id = drafted(client)
    before = stored(client, reminder_id).draft_text
    response = client.post(f"/api/reminders/{reminder_id}/tone", data={"tone": "custom"})
    assert response.status_code == 422
    assert stored(client, reminder_id).draft_text == before


def test_only_a_pending_draft_can_switch(client):
    reminder_id = drafted(client)
    client.post(f"/api/reminders/{reminder_id}/approve")
    sent_text = stored(client, reminder_id).draft_text
    response = client.post(f"/api/reminders/{reminder_id}/tone", data={"tone": "friendly"})
    assert response.status_code == 409
    reminder = stored(client, reminder_id)
    assert reminder.draft_text == sent_text
    assert reminder.chosen_tone is None


def test_a_rejected_draft_cannot_switch(client):
    reminder_id = drafted(client)
    client.post(f"/api/reminders/{reminder_id}/reject")
    response = client.post(f"/api/reminders/{reminder_id}/tone", data={"tone": "friendly"})
    assert response.status_code == 409


def test_another_users_draft_cannot_switch(client):
    reminder_id = drafted(client)
    before = stored(client, reminder_id)
    other = sign_in(TestClient(client.app), email="other@duebell.test")
    response = other.post(f"/api/reminders/{reminder_id}/tone", data={"tone": "friendly"})
    assert response.status_code == 404
    after = stored(client, reminder_id)
    assert after.draft_text == before.draft_text
    assert after.tone == before.tone
    assert after.chosen_tone is None


def test_a_hand_edited_draft_asks_before_it_is_replaced(client):
    reminder_id = drafted(client)
    client.post(f"/api/reminders/{reminder_id}/edit", data={"draft_text": "My own words."})
    response = client.post(f"/api/reminders/{reminder_id}/tone", data={"tone": "friendly"})
    assert response.status_code == 409
    assert response.json()["confirm"] is True
    assert stored(client, reminder_id).draft_text == "My own words."
    assert stored(client, reminder_id).tone == "custom"

    response = client.post(f"/api/reminders/{reminder_id}/tone", data={"tone": "friendly", "replace_edit": "true"})
    assert response.status_code == 200
    reminder = stored(client, reminder_id)
    assert FRIENDLY_ASK in reminder.draft_text
    assert reminder.tone == "friendly"
    assert reminder.chosen_tone == "friendly"


def test_editing_after_a_switch_keeps_the_chosen_tone(client):
    seed_invoice(client)
    with Session(client.app.state.engine) as session:
        reminder = draft_reminders(session, client.app.state.settings, scope=client.user_id)[0]
        switch_tone(session, reminder, "friendly", client.app.state.settings)
        edit_reminder(session, reminder, "Friendly, in my own words.")
        assert reminder.tone == "custom"
        assert reminder.chosen_tone == "friendly"


def test_the_dashboard_offers_both_tones_with_the_current_one_selected(client):
    reminder_id = drafted(client, risk="HIGH")
    page = client.get("/").text
    assert f'data-tone-for="{reminder_id}"' in page
    assert '<option value="friendly">Friendly</option>' in page
    assert '<option value="firm" selected>Firm</option>' in page

    client.post(f"/api/reminders/{reminder_id}/tone", data={"tone": "friendly"})
    page = client.get("/").text
    assert '<option value="friendly" selected>Friendly</option>' in page
    assert '<option value="firm">Firm</option>' in page


def test_the_dashboard_marks_a_hand_edited_draft(client):
    reminder_id = drafted(client)
    client.post(f"/api/reminders/{reminder_id}/edit", data={"draft_text": "My own words."})
    page = client.get("/").text
    assert 'data-edited="true"' in page
    assert '<option value="" selected disabled>Edited</option>' in page


def test_a_sent_reminder_offers_no_tone_switch(client):
    reminder_id = drafted(client)
    assert f'data-tone-for="{reminder_id}"' in client.get("/").text
    client.post(f"/api/reminders/{reminder_id}/approve")
    assert f'data-tone-for="{reminder_id}"' not in client.get("/").text


HARNESS = r"""
const vm = require("vm");
const fs = require("fs");
const [scriptPath, casesJson] = process.argv.slice(1);
const stub = () => new Proxy(function () {}, {
  get: (target, key) => key === "textContent" ? "[]" : key === Symbol.toPrimitive ? () => "" : stub(),
  apply: () => stub(),
  set: () => true,
});
const context = { document: stub(), window: {}, console };
vm.createContext(context);
const { toneSwitchOverwrites } = vm.runInContext(
  fs.readFileSync(scriptPath, "utf8") + "\n;({ toneSwitchOverwrites })",
  context,
);
const out = JSON.parse(casesJson).map(({ edited, value, saved }) =>
  toneSwitchOverwrites({ dataset: { edited } }, { value, defaultValue: saved }));
process.stdout.write(JSON.stringify(out));
"""


def test_the_dashboard_asks_before_a_switch_replaces_the_users_words():
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    cases = [
        {"edited": "false", "value": "Drafted.", "saved": "Drafted."},
        {"edited": "true", "value": "Mine.", "saved": "Mine."},
        {"edited": "false", "value": "Typed, not saved.", "saved": "Drafted."},
    ]
    result = subprocess.run(
        [node, "-e", HARNESS, str(SCRIPT), json.dumps(cases)],
        capture_output=True,
        text=True,
        timeout=30,
        check=True,
    )
    assert json.loads(result.stdout) == [False, True, True]


def test_an_older_database_gains_the_chosen_tone_column(tmp_path):
    url = f"sqlite:///{tmp_path / 'old.db'}"
    engine = make_engine(url)
    init_db(engine)
    with engine.begin() as connection:
        connection.execute(text('ALTER TABLE "reminder" DROP COLUMN "chosen_tone"'))
    init_db(create_engine(url))
    with engine.connect() as connection:
        columns = {row[1] for row in connection.execute(text('PRAGMA table_info("reminder")'))}
    assert "chosen_tone" in columns
